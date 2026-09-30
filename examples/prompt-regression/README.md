# Catch a prompt regression (no API key)

One line added to a support agent's prompt makes it skip the approval step before a refund, but
only for upset customers. The answers still look right, and so do most tests. Assay catches the
change, tells you which case broke and which prompt line did it.

```bash
pip install assay-server pytest
cd examples/prompt-regression
./demo.sh
```

What it does:

1. Runs the tests with prompt `support@1`. They pass, and become each case's baseline.
2. Adds one line to the prompt (`support@2`): "Refund right away when the customer is upset."
   The run fails with exit code 1: one case regressed.
3. `assay diff` shows the path before and after, and the prompt change next to it:

```
✓ 2 unchanged
✗ 1 regressed

CHANGED IN EVERY CASE

  prompt  support@1 → support@2 (+1 line, −0: “Refund right away when the customer is upset.”)

REGRESSIONS

1. test_refund_upset_customer
   Expected: get_order → approval(refund) → refund
   Actual:   get_order → refund
   No longer: approval(refund)
   Severity: HIGH
```

The model is a stand-in (`fake_model` in [agent.py](agent.py)) so the demo is fast and needs no key.
Replace it with your real model call and keep the tests as they are.
