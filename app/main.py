import asyncio
import json
import os
from pathlib import Path
import subprocess
import time

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates


REPO_DIR = Path(__file__).resolve().parents[1]
ZONEONE_DIR = REPO_DIR.parent
MODEL_DIR = Path(os.getenv("BONSAI_MODEL_DIR", ZONEONE_DIR / "models"))
LLAMA_PATH = Path(
    os.getenv(
        "BONSAI_LLAMA_COMPLETION",
        ZONEONE_DIR / "llama.cpp" / "build" / "bin" / "llama-completion",
    )
)

app = FastAPI(title="Bonsai Llama")
templates = Jinja2Templates(directory="app/templates")
REQUEST_TIMEOUT_SECONDS = 40
MAX_TOKENS = 900
MAX_GPU_LAYERS = 6
MAX_HISTORY_TURNS = 12
MAX_PROMPT_CHARS = 24000
DEFAULT_BEHAVIOR = (
    "Respond in a clear, well-formatted way. "
    "When explaining something, prefer numbered step-by-step structure such as Step 1, Step 2, Step 3 instead of one long paragraph. "
    "Use short sections, bullets, or code blocks when they make the answer clearer. "
    "If the user asks for only code or only the final answer, return just that."
)


def get_models() -> list[str]:
    if not MODEL_DIR.exists():
        return []
    return sorted(path.name for path in MODEL_DIR.glob("*.gguf"))


def build_chat_prompt(
    history: list[dict[str, str]],
    user_prompt: str,
) -> str:
    sections: list[str] = [f"System: {DEFAULT_BEHAVIOR}"]

    trimmed_history = history[-MAX_HISTORY_TURNS:]
    for message in trimmed_history:
        role = message.get("role", "").strip().lower()
        content = message.get("content", "").strip()
        if not content:
            continue
        if role == "user":
            sections.append(f"User: {content}")
        elif role == "assistant":
            sections.append(f"Assistant: {content}")

    sections.append(f"User: {user_prompt.strip()}")
    sections.append("Assistant:")

    prompt = "\n\n".join(sections)
    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[-MAX_PROMPT_CHARS:]
    return prompt


def build_command(
    prompt: str,
    model: str,
    n: int,
    temp: float,
    top_p: float,
    top_k: int,
    ngl: int,
) -> list[str]:
    model_path = MODEL_DIR / model
    safe_n = max(1, min(n, MAX_TOKENS))
    safe_ngl = max(0, min(ngl, MAX_GPU_LAYERS))

    return [
        str(LLAMA_PATH),
        "-m",
        str(model_path),
        "-p",
        prompt,
        "--no-display-prompt",
        "-n",
        str(safe_n),
        "--temp",
        str(temp),
        "--top-p",
        str(top_p),
        "--top-k",
        str(top_k),
        "-ngl",
        str(safe_ngl),
        "--simple-io",
    ]


def stream_line(payload: dict[str, str]) -> str:
    return json.dumps(payload) + "\n"


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "models": get_models(),
            "default_model": get_models()[0] if get_models() else None,
            "max_history_turns": MAX_HISTORY_TURNS,
            "max_prompt_chars": MAX_PROMPT_CHARS,
            "max_tokens": MAX_TOKENS,
            "default_behavior": DEFAULT_BEHAVIOR,
        },
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.post("/chat")
async def chat(
    request: Request,
    prompt: str = Form(...),
    history_json: str = Form("[]"),
    model: str = Form(...),
    n: int = Form(64),
    temp: float = Form(0.5),
    top_p: float = Form(0.9),
    top_k: int = Form(30),
    ngl: int = Form(0),
) -> StreamingResponse:
    if not LLAMA_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"llama-completion not found at {LLAMA_PATH}",
        )

    available_models = get_models()
    if model not in available_models:
        raise HTTPException(status_code=400, detail="Selected model was not found")

    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    try:
        history = json.loads(history_json)
        if not isinstance(history, list):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Chat history payload was invalid") from exc

    full_prompt = build_chat_prompt(history, clean_prompt)
    cmd = build_command(full_prompt, model, n, temp, top_p, top_k, ngl)

    print("Running command:", " ".join(cmd), flush=True)

    async def generate():
        process = None
        stdout_buffer = ""
        stderr_parts: list[str] = []
        streamed_any = False
        started_at = time.monotonic()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)

            while True:
                if await request.is_disconnected():
                    process.kill()
                    return

                if time.monotonic() - started_at > REQUEST_TIMEOUT_SECONDS:
                    process.kill()
                    yield stream_line(
                        {
                            "type": "error",
                            "content": (
                                f"llama-completion timed out after {REQUEST_TIMEOUT_SECONDS} seconds. "
                                "Try fewer tokens or GPU Layers = 0."
                            ),
                        }
                    )
                    return

                stdout_chunk = b""
                stderr_chunk = b""

                if process.stdout:
                    try:
                        stdout_chunk = os.read(process.stdout.fileno(), 4096)
                    except BlockingIOError:
                        stdout_chunk = b""

                if process.stderr:
                    try:
                        stderr_chunk = os.read(process.stderr.fileno(), 4096)
                    except BlockingIOError:
                        stderr_chunk = b""

                stdout_text = stdout_chunk.decode("utf-8", errors="replace")
                stderr_text = stderr_chunk.decode("utf-8", errors="replace")

                if stderr_text:
                    stderr_parts.append(stderr_text)

                if stdout_text:
                    streamed_any = True
                    stdout_buffer += stdout_text
                    yield stream_line({"type": "token", "content": stdout_text})

                if process.poll() is not None:
                    final_stdout = b""
                    final_stderr = b""

                    if process.stdout:
                        try:
                            final_stdout = os.read(process.stdout.fileno(), 4096)
                        except OSError:
                            final_stdout = b""

                    if process.stderr:
                        try:
                            final_stderr = os.read(process.stderr.fileno(), 4096)
                        except OSError:
                            final_stderr = b""

                    final_stdout_text = final_stdout.decode("utf-8", errors="replace")
                    final_stderr_text = final_stderr.decode("utf-8", errors="replace")

                    if final_stderr_text:
                        stderr_parts.append(final_stderr_text)

                    if final_stdout_text:
                        streamed_any = True
                        stdout_buffer += final_stdout_text
                        yield stream_line({"type": "token", "content": final_stdout_text})

                    if "".join(stderr_parts).strip():
                        print(
                            "llama-completion stderr:",
                            "".join(stderr_parts).strip(),
                            flush=True,
                        )

                    if process.returncode != 0:
                        yield stream_line(
                            {
                                "type": "error",
                                "content": (
                                    "".join(stderr_parts).strip()
                                    or "llama-completion failed"
                                ),
                            }
                        )
                        return

                    if not streamed_any or not stdout_buffer.strip():
                        yield stream_line(
                            {
                                "type": "error",
                                "content": (
                                    "".join(stderr_parts).strip()
                                    or "llama-completion returned an empty response"
                                ),
                            }
                        )
                        return

                    yield stream_line({"type": "done", "content": ""})
                    return

                await asyncio.sleep(0.05)
        except Exception as exc:
            if process and process.poll() is None:
                process.kill()
            if not await request.is_disconnected():
                yield stream_line({"type": "error", "content": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "X-Accel-Buffering": "no",
        },
    )
