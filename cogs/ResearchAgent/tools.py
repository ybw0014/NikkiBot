import asyncio
import copy
import datetime
import re
from typing import Any, AsyncGenerator, List, Tuple, Union, Optional

import openai
from googleapiclient.discovery import build  # Import the library
from gptfunctionutil import AILibFunction, GPTFunctionLibrary, LibParam
from langchain.docstore.document import Document


import gptmod
import gui
from gptmod.metadataenums import MetadataDocType
from gptmod.ReadabilityLoader import ReadableLoader
from utility import prioritized_string_split
from utility.debug import Timer

tosplitby = [
    # First, try to split along Markdown headings (starting with level 2)
    "\n#{1,6} ",
    # Note the alternative syntax for headings (below) is not handled here
    # Heading level 2
    # ---------------
    # End of code block
    "```\n",
    # Horizontal lines
    "\n\\*\\*\\*+\n",
    "\n---+\n",
    "\n___+\n",
    " #{1,6} ",
    # Note that this splitter doesn't handle horizontal lines defined
    # by *three or more* of ***, ---, or ___, but this is not handled
    "\n\n",
    "\n",
    " ",
    "",
]
symbol = re.escape("```")
pattern = re.compile(f"({symbol}(?:(?!{symbol}).)+{symbol})", re.DOTALL)

splitorder = [
    pattern,
    "\n# %s",
    "\n## %s",
    "\n### %s",
    "\n#### %s",
    "\n##### %s",
    "\n###### %s",
    "%s\n",
    "%s.  ",
    "%s. ",
    "%s ",
]


class MyLib(GPTFunctionLibrary):
    @AILibFunction(
        name="get_pdf_data",
        description="Return the title, authors, abstract, and date when given the first page of a PDF, if the info can be found.",
        enabled=True,
        force_words=["extract", "pdf"],
        required=["title"],
    )
    @LibParam(
        title="Title of the PDF given the first page.",
        authors="All authors of the PDF, given the first page.  If not available, pass in None",
        date="Date of publication of the PDF, in YYYY-MM-DD format.  You must return the Year, Month, and Day.  If it can't be found, return None.",
        abstract="Abstract found on the PDF.  If it can't be found, return not available.",
    )
    async def get_pdf_data(
        self,
        title: str,
        authors: str = "None",
        date: str = "None",
        abstract: str = "NA",
        **kwargs,
    ):
        # Wait for a set period of time.
        gui.gprint("extra kwargs", kwargs)
        return title, authors, date, abstract


async def try_until_ok(async_func, *args, **kwargs):
    """
    Attempts to run an asynchronous function up to  4 times.
    Example:
        completion = await try_until_ok(
                    asyncio.sleep(3),
                    timeout=60,
                )
    Args:
        async_func (Callable): The asynchronous function to run.
        *args: Positional arguments to pass to the asynchronous function.
        **kwargs: Keyword arguments to pass to the asynchronous function.

    Returns:
        Any: The result of the asynchronous function if it succeeds.

    Raises:
        Exception: If the asynchronous function fails after  4 attempts.
    """
    for tries in range(4):
        try:
            return await async_func(*args, **kwargs)
        except Exception as err:  # pylint: disable=broad-except
            if tries >= 3:
                raise err


def google_search(bot, query: str, result_limit: int) -> dict:
    query_service = build("customsearch", "v1", developerKey=bot.keys["google"])

    query_results = (
        query_service.cse()  # pylint: disable=no-member
        .list(q=query, cx=bot.keys["cse"], num=result_limit)  # Query  # CSE ID
        .execute()
    )

    gui.gprint(query_results)
    return query_results


async def async_markdown_convert(url, timeout=30):
    import markitdown

    markdown = markitdown.MarkItDown()
    result = await asyncio.wait_for(asyncio.to_thread(markdown.convert, url), timeout)
    return result


async def read_and_split_pdf(
    bot: Any, url: str, chunk_size: int, extract_meta: bool = False
) -> Tuple[List[Document], int]:
    try:
        result = await async_markdown_convert(url)
        metadata = {}
        new_docs: List[Document] = []
        title, authors, date, abstract = (
            result.title,
            "NotFound",
            "1-1-2020",
            "NotFound",
        )
        if extract_meta:
            mylib = MyLib()
            client = openai.AsyncClient()

            completion = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Given the raw text of the first page of a pdf, execute the get_pdf_data data function.",
                    },
                    {
                        "role": "user",
                        "content": f"Please extract the data for this pdf: {(result.text_content)[:2000]}",
                    },
                ],
                tools=mylib.get_tool_schema(),
                tool_choice="auto",
            )
            message = completion.choices[0].message
            if message.tool_calls:
                for tool in message.tool_calls:
                    typev: int = int(MetadataDocType.pdftext)
                    out: Optional[dict] = await mylib.call_by_tool_async(tool)
                    title, authors, date, abstract = out["content"]
                    break
        typev: int = int(MetadataDocType.pdftext)
        metadata["authors"] = authors
        metadata["website"] = "PDF_ORIGIN"
        metadata["title"] = title
        metadata["source"] = url
        metadata["description"] = abstract
        metadata["language"] = "en"
        metadata["dateadded"] = datetime.datetime.utcnow().timestamp()
        metadata["sum"] = "source"
        metadata["type"] = typev
        metadata["date"] = date

        newdata = copy.deepcopy(metadata)
        text = result.text_content
        # dealing with awkward spacing
        filtered_text = re.sub(r"-\s*\n", "", text)
        filtered_text = re.sub(r" +", " ", filtered_text)
        doc = Document(page_content=filtered_text, metadata=newdata)

        new_docs.append(doc)
        return new_docs, typev
    except Exception as ex:
        await bot.send_error(ex)
        return ex, -5


async def read_and_split_links(
    bot, urls: List[str], chunk_size: int = 1800, chunk_overlap: int = 1
) -> Tuple[List[Document], int]:
    # Document loader
    prioritysplit = []
    pdfsplit = []
    pdf_urls = []
    regular_urls = []
    symbol3 = re.escape("  ")
    pattern3 = re.compile(f"({symbol3}(?:(?!{symbol3}).)+{symbol3})", re.DOTALL)
    pdfsplit.append((pattern3, 100))
    for e, url in urls:
        if url.endswith(".pdf") or ".pdf" in url:
            pdf_urls.append((e, url))
        else:
            regular_urls.append((e, url))

    # PDF LINKS
    for e, pdfurl in pdf_urls:
        pdfmode = True
        newdata = []
        with Timer() as timer:
            data, typev = await read_and_split_pdf(bot, pdfurl, chunk_size)
        gui.gprint(f"PDF READ: Took {timer.get_time():.4f} seconds to READ pdf {e}.")

        if isinstance(data, Exception):
            yield data, e, typev
            continue
        splitnum = 0
        for d in data:
            with Timer() as timer:
                newdat = await simplify_and_split_output(
                    d, chunk_size, pdfsplit, splitnum
                )
            gui.gprint(
                f"PDF: Took {timer.get_time():.4f} seconds to convert {e} into {len(newdat)} splits."
            )

            splitnum += len(newdat)
            newdata.extend(newdat)
        yield newdata, e, typev

    loader = ReadableLoader(
        regular_urls,
        header_template={
            "User-Agent": "Mozilla/5.0 (X11,Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        },
    )
    # Index that wraps above steps
    async for d, e, typev2 in loader.aload(bot):
        gui.gprint(type(d), e, typev2)
        if typev2 == -5:
            yield d, e, typev2
        else:
            with Timer() as timer:
                newdata = await simplify_and_split_output(d, chunk_size, prioritysplit)
            gui.gprint(
                f"READABILITY: Took {timer.get_time():.4f} seconds to convert {e} into {len(newdata)} splits."
            )

            yield newdata, e, typev2


async def read_and_split_link(
    bot, url: str, chunk_size: int = 1800, chunk_overlap: int = 1
) -> Tuple[Union[List[Document], Exception], int, MetadataDocType]:
    # Document loader
    prioritysplit = []

    if url.endswith(".pdf") or ".pdf?" in url:
        pdfmode = True
        symbol3 = re.escape("  ")
        pattern3 = re.compile(f"({symbol3}(?:(?!{symbol3}).)+{symbol3})")
        prioritysplit.append((pattern3, 100))
        data, typev = await read_and_split_pdf(bot, url, chunk_size)
        newdata = []
        splitnum = 0
        if data is None:
            return None, 0, typev

        for d in data:
            newdat = await simplify_and_split_output(
                d, chunk_size, prioritysplit, splitnum
            )
            splitnum += len(newdat)
            newdata.extend(newdat)
        return newdata, 0, typev

    else:
        loader = ReadableLoader(
            [(0, url)],
            header_template={
                "User-Agent": "Mozilla/5.0 (X11,Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
            },
        )
        # Index that wraps above steps
        async for d, e, typev2 in loader.aload(bot):
            gui.gprint(type(d), e, typev2)
            if typev2 == -5:
                return d, e, typev2
            newdata = await simplify_and_split_output(d, chunk_size, prioritysplit)
            return newdata, e, typev2


async def simplify_and_split_output(
    d: Document, chunk_size, prioritysplit, split_num=0
):
    newdata = []
    splitnum = split_num
    simplified_text = d.page_content.strip()
    simplified_text = re.sub(r"(\n){4,}", "\n\n\n", simplified_text)
    simplified_text = re.sub(r" {3,}", "  ", simplified_text)
    simplified_text = simplified_text.replace("\t{3,}", "\t")
    simplified_text = re.sub(r"\n+(\s*\n)*", "\n", simplified_text)
    d.page_content = simplified_text
    split, splitnum = await asyncio.to_thread(
        split_link, d, chunk_size=chunk_size, prior=prioritysplit, add=splitnum
    )
    newdata.extend(split)
    return newdata


def split_link(doc: Document, chunk_size: int = 1800, prior=[], add=0):
    newdata = []

    metadata = doc.metadata
    tosplitby = prior
    tosplitby.extend(splitorder)
    fil = prioritized_string_split(
        doc.page_content, tosplitby, default_max_len=chunk_size
    )

    for e, chunk in enumerate(fil):
        metadatac = copy.deepcopy(metadata)

        metadatac["split"] = add
        add += 1
        new_doc = Document(page_content=chunk, metadata=metadatac)
        newdata.append(new_doc)
    return newdata, add


def set_ranges_based_on_token_size(tokens):
    ranges_dict = {
        250: ("4-7", "7-10", "3-4"),
        500: ("5-8", "8-12", "4-5"),
        1000: ("6-9", "9-14", "5-6"),
    }
    for size, ranges in sorted(ranges_dict.items()):
        if tokens < size:
            return ranges
    return ("7-10", "8-16", "6-7")


summary_prompt_old = """
    Summarize general news articles, forum posts, and wiki pages that have been converted into Markdown. 
    Condense the content into 2-5 medium-length paragraphs with 5-10 sentences per paragraph. 
    Preserve key information and maintain a descriptive tone.
    Exclude any concluding remarks.
"""

summary_prompt = """
    As a professional summarizer, create a concise and comprehensive summary of the provided text, 
    be it an article, post, conversation, or passage, while adhering to these guidelines:
    * Craft a summary that is detailed, thorough, in-depth, and complex, while maintaining clarity and conciseness.
    * Incorporate main ideas and essential information, eliminating extraneous language and focusing on critical aspects.
    * Rely strictly on the provided text, without including external information.
    * Format the summary into 2-5 medium-length paragraphs with 5-10 sentences per paragraph.
    * Large texts WILL be split up, but you will not be given the other parts of the text.

"""


async def summarize(
    prompt: str, article: str, mylinks: List[Tuple[str, str]]
) -> AsyncGenerator[str, None]:
    client = openai.AsyncOpenAI()

    def local_length(st: str) -> int:
        return gptmod.util.num_tokens_from_messages(
            [
                {"role": "system", "content": summary_prompt + prompt},
                {"role": "user", "content": st},
            ],
            "gpt-4o-mini",
        )

    result: str = ""
    fil = prioritized_string_split(article, splitorder, 20000, length=local_length)
    filelength: int = len(fil)
    for num, articlepart in enumerate(fil):
        gui.gprint("summarize operation", num, filelength)
        messages = [
            {
                "role": "system",
                "content": f"{summary_prompt}\n{prompt}\n You are viewing part {num + 1}/{filelength} ",
            },
            {"role": "user", "content": f"\n {articlepart}"},
        ]
        completion = await try_until_ok(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=messages,
            timeout=60,
        )

        result = completion.choices[0].message.content
        for link in mylinks:
            link_text, url2 = link
            link_text = link_text.replace("_", "")
            gui.dprint(link_text, url2)
            if link_text in result:
                gui.dprint(link_text, url2)
                # sources.append(f"[{link_text}]({url})")
                result = result.replace(link_text, f"{link_text}")
        yield result


def extract_embed_text(embed):
    """
    Extracts the text from an embed object and formats it as a bullet list.

    Args:
        embed (Embed): The embed object to extract text from.

    Returns:
        str: A string containing the title, description, and fields of the embed, formatted as a bullet list.
    """
    bullet_list = []

    # Extract title, description, and fields from the Embed
    if embed.title:
        bullet_list.append(f"{embed.title}")

    if embed.description:
        bullet_list.append(f"{embed.description}")

    for field in embed.fields:
        bullet_list.append(f"**{field.name}**: {field.value}")

    # Join the extracted text with bullet points
    bullet_string = "\n".join([f"• {line}" for line in bullet_list])
    return bullet_string


def mask_links(text, links):
    # Split links by newline
    link_lines = links.strip().split("\n")
    links_dict = {}

    # Extract numbers for each element in newline
    for line in link_lines:
        # gui.gprint(line)
        match = re.match(r"\[([\d, ]+)\](https?://[^\s]+)", line)
        if match:
            numbers, url = match.groups()
            for number in map(int, numbers.split(",")):
                links_dict[number] = url

    # Replace occurrences of [number] with masked links
    for number, url in links_dict.items():
        # gui.gprint(number, url)
        num_pattern = re.compile(rf"\[({number})\]")
        text = re.sub(num_pattern, f"[{number}]({url})", text)

    return text


async def format_answer(question: str, docs: List[Tuple[Document, float, Any]]) -> str:
    """
    Formats an answer to a given question using the provided documents.

    Args:
        question (str): The question to answer.
        docs (List[Tuple[Document, float]]): A list of tuples containing Document objects and relevance scores.

    Returns:
        str: The formatted answer as a string.
    """

    prompt = """

**Task:**
Use the provided sources, 
presented in individual system messages with relevant snippets, 
to craft a comprehensive response to the user's question. 
Each source is formatted in the following template:

```
BEGIN
**ID:** [Source ID number here]
**Name:** [Name Here]
**Link:** [Link Here]
**Text:** [Text Content Here]
END
```

**Guidelines:**
1. Your response should consist of 3-7 medium-length paragraphs, containing 6-12 sentences each.
2. Preserve crucial information from the sources and maintain an objective, descriptive tone in your writing.  
3. Ensure the inclusion of an inline citation for each piece of information obtained from a specific source,
   using this format: 
    Sentence goes here[0].
   **This is crucially important, as the inline citations are used to verify the response accuracy.**
4. If the sources do not provide sufficient information on a particular aspect of the question, explicitly state this limitation.
5. Do not write a conclusion under any circumstance.  This is not an essay.
6. Do not include the terms "summary", "conclusion", or "overall" in the response.
7. Do not utilize superfluous language.
    """
    # The websites may contradict each other, prioritize information from encyclopedia pages and wikis.
    # Valid news sources follow.
    # Your goal is not to summarize, your goal is to answer the user's question based on the provided sources.
    formatted_docs = []
    messages = [
        {"role": "system", "content": prompt},
    ]

    total_tokens = gptmod.util.num_tokens_from_messages(
        [{"role": "system", "content": prompt}, {"role": "user", "content": question}],
        "gpt-4o-mini",
    )
    for e, tup in enumerate(docs):
        doc, _ = tup

        meta = doc.metadata
        content = doc.page_content
        tile = "NOTITLE"
        if "title" in meta:
            tile = meta["title"]
        output = f"""**ID**:{e}
        **Name:** {tile}
        **Link:** {meta["source"]}
        **Text:** {content}"""
        formatted_docs.append(output)

        tokens = gptmod.util.num_tokens_from_messages(
            [{"role": "system", "content": output}], "gpt-4o-mini"
        )

        if total_tokens + tokens >= 14000:
            gui.gprint("token break")
            break
        total_tokens += tokens

        messages.append({"role": "system", "content": output})
        if total_tokens >= 14000:
            gui.gprint("token break")
            break
    messages.append({"role": "user", "content": question})
    client = openai.AsyncOpenAI()
    for tries in range(0, 4):
        try:
            completion = await client.chat.completions.create(
                model="gpt-4o-mini", messages=messages, timeout=60
            )
            return completion.choices[0].message.content
        except Exception as e:
            if tries >= 3:
                raise e


def get_doc_sources(docs: List[Tuple[Document, float]]):
    """
    Takes a list of Document objects, counts the appearances of unique sources amoung them,
    and return a string indicating the used sources.

    Args:
        docs (List[Tuple[Document,float]]): A list of tuples containing Document objects and their associated float score.

    Returns:
        str: A string formatted to list unique sources and the indices of their appearances in the provided list.
    """
    all_links = [doc.metadata.get("source", "???") for doc, e in docs]
    links = set(doc.metadata.get("source", "???") for doc, e in docs)

    def ie(all_links: List[str], value: str) -> List[int]:
        return [index for index, link in enumerate(all_links) if link == value]

    used = "\n".join(f"{ie(all_links, l)}{l}" for l in links)
    source_pages = prioritized_string_split(used, ["%s\n"], 4000)
    cont = ""
    if len(source_pages) > 2:
        new = "\n"
        cont = f"...and {sum(len(se.split(new)) for se in source_pages[1:])} more."
    source_string = f"{source_pages[0]}{cont}"
    return source_string, used
