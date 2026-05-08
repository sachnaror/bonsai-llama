import os
from pathlib import Path
import subprocess

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
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


def get_models() -> list[str]:
    if not MODEL_DIR.exists():
        return []
    return sorted(path.name for path in MODEL_DIR.glob("*.gguf"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "models": get_models(),
            "default_model": get_models()[0] if get_models() else None,
        },
    )


@app.post("/chat")
def chat(
    prompt: str = Form(...),
    model: str = Form(...),
    n: int = Form(300),
    temp: float = Form(0.5),
    top_p: float = Form(0.9),
    top_k: int = Form(30),
    ngl: int = Form(0),
) -> dict[str, str]:
    if not LLAMA_PATH.exists():
        raise HTTPException(
            status_code=500,
            detail=f"llama-completion not found at {LLAMA_PATH}",
        )

    available_models = get_models()
    if model not in available_models:
        raise HTTPException(status_code=400, detail="Selected model was not found")

    model_path = MODEL_DIR / model
    cmd = [
        str(LLAMA_PATH),
        "-m",
        str(model_path),
        "-p",
        prompt,
        "--no-display-prompt",
        "-n",
        str(n),
        "--temp",
        str(temp),
        "--top-p",
        str(top_p),
        "--top-k",
        str(top_k),
        "-ngl",
        str(ngl),
        "--simple-io",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                "llama-completion timed out after 180 seconds. "
                "Try a smaller token count, a smaller model, or check server logs."
            ),
        ) from exc

    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=(result.stderr or "llama-completion failed").strip(),
        )

    response_text = result.stdout.strip()
    if not response_text:
        raise HTTPException(
            status_code=500,
            detail=(result.stderr or "llama-completion returned an empty response").strip(),
        )

    return {"response": response_text}
