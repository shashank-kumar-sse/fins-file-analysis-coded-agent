import os
from io import BytesIO
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import START, END, StateGraph
from pydantic import BaseModel, Field
from uipath.platform import UiPath
from uipath.platform.attachments import Attachment
from uipath.platform.common import UiPathConfig
from uipath_langchain.chat import UiPathChat
from prompts import prompts


llm = UiPathChat(model="gpt-4o-mini-2024-07-18")
MAX_PDF_TEXT_CHARS = 120_000


class Input(BaseModel):
    pdf_file: Attachment = Field(
        ..., title="PDF File", description="PDF to analyze"
    )


class State(BaseModel):
    pdf_file: Attachment
    report: str = ""


class Output(BaseModel):
    report: str


def extract_pdf_text(pdf_bytes: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(pdf_bytes))
    pages = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"--- Page {index} ---\n{text.strip()}")

    extracted_text = "\n\n".join(pages).strip()
    if not extracted_text:
        raise ValueError(
            "No selectable text was found in the PDF. If this is a scanned PDF, run OCR first."
        )

    return extracted_text[:MAX_PDF_TEXT_CHARS]


async def read_pdf_bytes(pdf_file: Attachment) -> bytes:
    # Local runs can't upload attachments; read from UIPATH_LOCAL_ATTACHMENT instead.
    if UiPathConfig.job_key is None and os.environ.get("UIPATH_LOCAL_ATTACHMENT"):
        pdf_path = Path(os.environ["UIPATH_LOCAL_ATTACHMENT"]).expanduser().resolve()
        if not pdf_path.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")
        return pdf_path.read_bytes()

    if pdf_file.mime_type and pdf_file.mime_type.lower() != "application/pdf":
        raise ValueError(f"Expected a PDF file, received '{pdf_file.mime_type}'.")

    uipath = UiPath()
    async with uipath.attachments.open_async(attachment=pdf_file) as (
        _attachment,
        response,
    ):
        return b"".join([chunk async for chunk in response.aiter_bytes()])


async def analyze_pdf(pdf_bytes: bytes) -> str:
    pdf_text = extract_pdf_text(pdf_bytes)

    system_prompt = (
        prompts.business_financial_statement_analysis
    )
    output = await llm.ainvoke(
        [
            SystemMessage(system_prompt),
            HumanMessage(f"\n\n{pdf_text}"),
        ]
    )
    return output.content


async def generate_report(state: State) -> State:
    report = await analyze_pdf(await read_pdf_bytes(state.pdf_file))
    return State(pdf_file=state.pdf_file, report=report)


async def output_node(state: State) -> Output:
    return Output(report=state.report)


builder = StateGraph(State, input_schema=Input, output_schema=Output)

builder.add_node("generate_report", generate_report)
builder.add_node("output", output_node)

builder.add_edge(START, "generate_report")
builder.add_edge("generate_report", "output")
builder.add_edge("output", END)

graph = builder.compile()


# Local entry point: takes a PDF file path instead of a job attachment.
class LocalInput(BaseModel):
    pdf_path: str = Field(
        ..., title="PDF Path", description="Local path to the PDF to analyze"
    )


class LocalState(BaseModel):
    pdf_path: str
    report: str = ""


async def generate_report_local(state: LocalState) -> LocalState:
    pdf_path = Path(state.pdf_path).expanduser().resolve()
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF file not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file, received '{pdf_path.name}'.")

    report = await analyze_pdf(pdf_path.read_bytes())
    return LocalState(pdf_path=state.pdf_path, report=report)


async def output_node_local(state: LocalState) -> Output:
    return Output(report=state.report)


local_builder = StateGraph(LocalState, input_schema=LocalInput, output_schema=Output)

local_builder.add_node("generate_report", generate_report_local)
local_builder.add_node("output", output_node_local)

local_builder.add_edge(START, "generate_report")
local_builder.add_edge("generate_report", "output")
local_builder.add_edge("output", END)

local_graph = local_builder.compile()
