"""A support agent with a stand-in for the model, so the demo needs no API key.

The stand-in follows its prompt the way a real model would follow these lines: it looks up the
order, asks for approval before a refund, and skips the approval when the prompt tells it to
refund right away for an upset customer. Swap `fake_model` for your real model call.
"""
import os
from pathlib import Path

import assay_sdk as assay

PROMPTS = Path(__file__).parent / "prompts"
ORDERS = {"O-17": {"price": 27.61, "status": "delivered"}, "O-18": {"price": 12.00, "status": "shipped"}}


def load_prompt():
    """The prompt version to use: ASSAY_DEMO_PROMPT, 1 by default. Registered so a regression shows
    what changed in the prompt next to it."""
    version = os.environ.get("ASSAY_DEMO_PROMPT", "1")
    text = (PROMPTS / f"support-{version}.txt").read_text()
    return assay.prompt("support", version, template=text), text


def get_order(order_id):
    return ORDERS[order_id]


def refund(order_id, amount):
    return {"refunded": amount}


def fake_model(prompt, message):
    """What the model decides to do. Replace with your real model call."""
    upset = any(w in message.lower() for w in ("now", "angry", "ridiculous"))
    skip_approval = upset and "refund right away" in prompt.lower()
    return {"skip_approval": skip_approval}


def support_agent(run, message, order_id):
    prompt_ref, prompt = load_prompt()
    decision = fake_model(prompt, message)
    run.llm(model="demo-model", prompt=prompt_ref, tokens_in=850, tokens_out=60, cost_usd=0.0021,
            tools=["get_order", "refund"])
    order = run.call("get_order", get_order, order_id=order_id)
    if order["status"] != "delivered":
        reply = f"Order {order_id} hasn't arrived yet, so it can't be refunded."
    else:
        if not decision["skip_approval"]:
            run.approval("refund", "approved", by="policy:under-50")
        run.call("refund", refund, order_id=order_id, amount=order["price"])
        reply = f"Refunded ${order['price']:.2f}."
    run.answer(reply)
    run.outcome("resolved")
    return reply
