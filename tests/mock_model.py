"""Mock model server — echoes back a simple response for testing."""

from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
import uvicorn

app = FastAPI()


class Message(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "mock"
    messages: List[Message]
    temperature: Optional[float] = 0
    max_tokens: Optional[int] = 2048


@app.post("/v1/chat/completions")
async def chat(request: ChatRequest):
    # Simple mock: if question contains math, return the answer
    user_msg = request.messages[-1].content.lower()
    if "1 + 1" in user_msg or "1+1" in user_msg:
        answer = "The answer is 2."
    elif "2 * 3" in user_msg or "2*3" in user_msg:
        answer = "The answer is 6."
    elif "10 - 4" in user_msg or "10-4" in user_msg:
        answer = "The answer is 6."
    elif "def " in user_msg or "function" in user_msg:
        answer = "def add(a, b):\n    return a + b"
    else:
        answer = f"Echo: {request.messages[-1].content}"

    return {
        "id": "mock-001",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
