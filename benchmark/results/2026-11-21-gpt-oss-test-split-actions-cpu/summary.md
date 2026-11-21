# Planted-bug benchmark

20 real merged PRs, each with one bug planted on a line it adds. Each model reviewed
them as the Action does: the `small` profile and an 8,192-token window, unless a variant says
otherwise. Only the model's own findings count. A finding is near the bug when it's on that file
within 3 lines; found means it was read and it describes the bug. Where the PRs were also
reviewed as merged (the clean control), *near only with the bug* counts the cases whose clean
version got no finding on that line: a model that comments on every changed line doesn't score.

| Model | Variant | PRs | Near the bug | Near it when clean | Near only with the bug | Found (dev + test) | Other findings | Other findings marked right | Findings on the clean PRs | Failed calls | Median time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `gpt-oss:20b` | logic-context10-verify-strict | 15 | 11 of 15 | 0 of 20 | 11 of 15 | 11 of 15 (0 + 11) | 7 | 1 of 3 marked | 7 | 2 of 125 | 646s |
| `gpt-oss:20b` | reasoning-low-verify-strict | 20 | 5 of 20 | 0 of 20 | 5 of 20 | 5 of 20 (0 + 5) | 3 | 1 of 1 marked | 3 | 2 of 57 | 199s |

Found, by kind of bug:

| Model | Variant | flipped-comparison | inverted-return | missing-await | negated-condition | off-by-one | wrong-boolean |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gpt-oss:20b` | logic-context10-verify-strict | 3 of 4 | 1 of 1 | 1 of 2 | 3 of 4 | 0 of 1 | 3 of 3 |
| `gpt-oss:20b` | reasoning-low-verify-strict | 1 of 5 | 0 of 2 | 1 of 2 | 3 of 5 | 0 of 2 | 0 of 4 |

**6 finding(s) not marked yet**, so Found and the marked counts are incomplete.

Left out: encode/httpx#3670. Not a bug: the stream's file comes from the builtin open(), so close() is synchronous and dropping its await fixes the line rather than breaking it. The merged PR awaits that file's read() and close() (lines 199, 205, 217), which raises TypeError on httpx's v1 branch.
