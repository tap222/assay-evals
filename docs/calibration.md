[Assay](../README.md) › [Documentation](README.md)

# Judge calibration: does the judge agree with a person?

A judge can return valid JSON, in range, every time, and still be wrong. It can rank a great
answer below a poor one, call everything a 3, or score the same answer 2 and then 4. The checks
elsewhere catch a judge that breaks (an answer that isn't a verdict, the wrong inputs, a judge
that disagrees with itself). Only a golden set catches one that's miscalibrated: outputs a
person scored, spanning terrible to great.

Most of the value is in the golden set, not in the metric. So Assay helps build the set, and
then checks the judge against it on every prompt or model change.

## The golden set

`golden.jsonl`, in the repository, one item a line:

```json
{"id": "q17", "input": "Tell me about a launch that failed", "output": "...", "score": 4, "by": "sam", "tags": ["behavioral"]}
{"id": "q18", "input": "...", "output": "...", "labels": [{"by": "sam", "score": 2}, {"by": "ana", "score": 3}]}
```

An item's label is the median of its labels. `tags` group items (a question type, say), so each
group's calibration is checked on its own.

```
assay golden add tests/ai/test_coach.py::test_star_answer --score 4    # a recorded output, labeled
assay golden add q17 --score 3 --by ana                                # a second person's label
assay golden suggest --field helpful                                   # what to label next
assay golden stats
```

`golden suggest --vs helpful_gpt` picks the runs two judges scored furthest apart, and
`--disagree` the ones the judge passed but a deterministic check failed. Where evaluators
disagree says more about a judge's bias than where they agree, so those are the items to
label first.

`golden add` takes the input and output from the latest recorded run of that case (or
`--input`/`--output`). `golden suggest` picks recorded outputs spread over what the judge scored
them, so the set spans poor to great instead of piling up typical answers. `golden stats` shows
labels per score (`nothing labeled 1: the judge is untested there`), who labeled, and, for items
two people labeled, how much they agree. That agreement is the ceiling: no judge will match a
person much better than two people match each other.

A label can carry a critique, what's wrong in words, the way a domain expert would explain it
to someone new: `assay golden add q17 --score 1 --critique "Quotes the refund policy; they asked
where the order is."` Critiques are what a judge learns from, and what a person reads when the
judge disagrees.

### Splits: examples from train, measured on dev and test

A judge prompt with examples taken from the items it's then measured on says nothing about unseen
data. `assay golden split` assigns each item to train, dev or test (20%, 40%, 40% by default,
stratified by label, so every label is in each, and seeded so it's the same every time; items
already split keep theirs):

```
assay golden split                    # --train 0.2 --dev 0.4 --seed 0
assay calibrate                       # on dev: iterate on the judge here
assay calibrate --final               # on test, once, for the number you report
```

A judge takes its few-shot examples from train:

```python
from assay_sdk import golden_examples
SHOTS = golden_examples(split="train", k=8)   # [{"id", "input", "output", "label", "critique", "tags"}]
```

`--by tags` (or `--by input`) keeps whole groups together: every item with the same tags, or every
answer to the same input, goes to one split. Then dev and test are topics the judge's examples never
came from, which is the question to ask when traffic can shift to kinds of request the golden set
doesn't have yet. A new item of a group that's already split joins its group's split.

```
assay golden split --by tags
```

Calibration checks for a leak and fails on one: the judge asked for the split it's measured on
(`golden_examples(split="dev")`), or an item's output from that split is in the judge's source
file. An unsplit golden set is calibrated as a whole, with a reminder to split it.

How big? The report answers it for your set. The Spearman interval narrows as items are added:
at 30 items it's wide, and at 100 it can tell 0.8 from 0.9. Label every score level, and have a
second person label 20 or so items.

## Calibrating

```toml
[calibrate]
judge = "evals/judges.py:helpfulness"   # or "evals.judges:helpfulness": called as judge(input, output)
repeat = 5                              # judgements per item: how much it swings
score_range = [1, 5]                    # the judge's scale
label_range = [1, 5]                    # the labels' (default: the same)
threshold = 3                           # pass at or above, for pass/fail flips
min_drop = 0.05                         # smaller drops in rank correlation are reported, not failed
field = "helpful"                       # the judge's check in your tests, for `golden suggest`
group_by = "tags"                       # also ranked within each tag; "input": answers to the same input
```

The judge is anything `evaluate()` takes: a function returning a score, a dict, JSON text, or an
`assay_sdk.Judge` answer. It runs through `EvalRuntime`, so concurrency, retries and cost apply.

```
$ assay calibrate
Judge calibration  c-20260926-125520-210
────────────────────────────────────────────
evals/judges.py:helpfulness · 60 items · 5 judgements each

Golden set   60 items, labeled by sam (60), ana (20)
             1: 10  2: 12  3: 14  4: 14  5: 10
             people agree: exact 70%, within one 95% (20 items labeled twice): the ceiling for any judge

Ranking      Spearman 0.82 (95% interval 0.71–0.89)
             pairs in the right order: 94% (1,334 of 1,420)
             2 ordering violations two or more apart:
               q17 labeled 5, judged 2.0  <  q04 labeled 3, judged 3.0
Agreement    exact 58%, within one 92%
Bias         +0.40 (lenient) · labeled 1 → judged 2.1 on average
Consistency  mean spread 0.40 across 5 judgements · 4 flip pass/fail: q09, q22, q31, q40
Validity     60 of 60 items judged
By tag       behavioral 0.88 (n=30)   product_sense 0.71 (n=30)

Compared with c-20260919-101204-551 (60 items in both)
  overall        Spearman 0.86 → 0.82 (-0.04, within chance)
  behavioral     Spearman 0.84 → 0.88 (+0.04)
✗ product_sense  Spearman 0.87 → 0.71 (-0.16, beyond chance)

Regressed.
```

- **Ranking:** Spearman rank correlation with the labels, with its 95% interval (a bootstrap over
  the items), the share of pairs in the right order, and the ordering violations themselves.
- **Agreement:** exact and within one, and a label × judge table.
- **Bias:** lenient or harsh on average, the label it's furthest off on, and whether it squashes
  every answer toward the middle.
- **Consistency:** how far each item's score swings across the repeats, and which items flip
  between pass and fail.
- **Validity:** answers that weren't verdicts, and timeouts, are counted apart. They never
  become scores.
- **Within tags:** the same ranking inside each tag (below).

## Ranking answers, or recognizing the topic?

A golden set's tags often differ in typical quality: refund answers rated low, greetings high. A
judge that only recognizes the topic, and gives every refund answer a 2 and every greeting a 5,
then gets a healthy Spearman overall, from telling tags apart. Among answers of the same tag it
ranks nothing. Two more numbers tell the two judges apart:

- **Within tags:** Spearman over the items centred on their tag's mean, the label and the judge's
  score alike. It asks how well the judge ranks answers of the same kind. Its interval resamples
  whole tags, since the tag is the unit here, not the item.
- **Knowing only the tag:** Spearman of each label against the average label of the other items
  in its tag. It's what the tags alone reach, with no judge at all.

When the tags alone reach 0.3 or more, the judge ranks below 0.3 within them, and its overall
number is at least 0.2 above that, calibration says so:

```
Ranking      Spearman 0.63 (95% interval 0.42–0.78)
...
Within tags  Spearman 0.00 (95% interval 0.00–0.00, 4 tags resampled) · knowing only each tag's average label: 0.34
             it tracks the topic, not the answer: 0.63 overall comes from telling tags apart. Among answers of the same tag it ranks at 0.00
```

It isn't a failed calibration: nothing got worse. It's a trust problem, and every score from
that judge carries it ([below](#every-judged-number-says-whether-it-can-be-trusted)). The fix is
in the judge: its rubric or examples lean on the kind of request, not the quality of the answer.

The check needs 3 tags or more with 3 judged items each. Items with several tags are grouped by
the combination. `group_by = "input"` groups answers to the same input instead (several outputs
labeled per question), and `group_by = "none"` turns it off.

## Bias: what the judge rewards besides quality

A judge's score mixes quality with what the judge happens to like. Calibration separates the two
in two ways.

**Catch rate.** Judges confirm good answers far more reliably than they catch bad ones:

```
Catch rate   fails 3 of the 12 answers people called bad (25%, 95% interval 9%–53%); passes 40 of 42 good ones: it confirms good answers but lets bad ones through
```

"Bad" is a label below the pass mark (`threshold`, on the labels' scale). A bad answer it caught
before and passes now counts against it, and more of those than the reverse, beyond chance (a
sign test), fails the calibration.

**Bias probes.** For each item, the gap between the judge's score and the person's. The probes
check whether that gap follows a surface feature, and report what's beyond chance:

```
Bias probe   longer answers score higher than people scored them (Spearman 0.41 between length and the gap)
Bias probe   answers with citations or links score 1.2 more than people scored them, compared with the rest (14 vs 16 items)
```

The features are length, citations or links, hedging ("might", "I'm not sure"), and headers or
bullets. With `"model": "claude-sonnet-5"` on items (which model wrote the output), a judge that
favors its own family shows up the same way. Variants test one bias directly: the same answer
with fake citations, `expect: "same"`.

## Variants: does the score move for the right reasons?

A judge you can trust gives a paraphrase the same score and a subtly wrong answer a lower one.
Give golden items variants, each with what should happen to its score:

```json
{"id": "q17", "input": "...", "output": "...", "score": 4, "variants": [
  {"output": "the same answer, reworded", "expect": "same", "note": "paraphrase"},
  {"output": "the same answer, with the refund window wrong", "expect": "lower", "note": "wrong fact"}]}
```

```
Variants     11 of 12 as expected: paraphrases kept their score 6/6, broken versions scored lower 5/6
               q17 (wrong fact): 4.0 → 4.0, should score lower
```

"Same" allows for the noise the item already shows across repeats. You write the variants, so
nothing is generated and the check is repeatable. A broken variant that used to score lower
and now doesn't fails the calibration: the judge can no longer tell a wrong answer from a right
one.

## A second judge

```toml
[calibrate]
second_judge = "evals/judges.py:helpfulness_gpt"   # or: assay calibrate --second-judge ...
```

Another judge over the same items says how often two judges agree, which of them tracks people
better, and where they disagree by two points or more. That's usually where the rubric is
ambiguous, and it's the first place to tighten it.

## Drift: the same judge, scoring differently

A provider can change a model without changing its name. A judge that became stricter overnight
still returns valid verdicts, so the scores just move. Each calibration records the judge's code
(a digest of its file) and the models that answered. When a calibration regresses with the same
code, the same model names and the same golden set, the report says so:

```
Why: the same judge (its code and its model's name) over the same items agrees with people less than it did: the provider changed the model under its name.
```

Only a calibration run with nothing else changed catches this, so run it on a schedule:

```yaml
on:
  schedule: [{cron: "0 6 * * *"}]   # every morning
jobs:
  judge-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install assay-server && assay calibrate
```

## As a regression test

Each calibration is stored and compared with the last one that passed, over the items both
judged: overall and per tag. Improving one kind of item often breaks another. It fails (exit 1)
when:

- a rank correlation dropped beyond chance (a paired bootstrap over the same items) and by at
  least `min_drop`, overall or for a tag with 8 items or more;
- a new ordering violation is two or more label points apart: a 5 now judged below a 2.

Violations one point apart, a lean, and a wider spread are reported, not failed. Run it where
the judge's prompt or model changes:

```yaml
- run: assay calibrate
```

When the judge's model or prompt changes, `assay test` doesn't compare the new judge's results with
the old one's baseline as if only the AI had changed ([When the judge or the model
changes](testing.md#when-the-judge-or-the-model-changes)). Calibrating the new judge is how to
know whether to trust it.

## Every judged number says whether it can be trusted

`assay test` lists each judged check with its calibration, so a score is never read without
knowing whether anyone checked the judge against people:

```
Judges
  helpful      calibrated 3 days ago: Spearman 0.82 on 60 items (claude-opus-5)
  consistency  not calibrated: its scores haven't been checked against people (`assay calibrate`)
```

`[calibrate] field` says which check the calibrated judge is. A calibration older than 30 days is
marked stale, one that regressed says not to lean on the scores, and one made for another judge
model than the one that scored this run says so, and one that tracks the topic rather than the
answer says that (`calibrated today: Spearman 0.63 on 48 items, but it tracks the topic, not the
answer: Spearman 0.00 among answers of the same tag`). The PR comment has the same list.

`assay calibrate --baseline none` starts over, `--baseline ID` compares with a given one, and
`--format json` gives it all as data.
