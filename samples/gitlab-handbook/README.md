# Phase 1 pilot: GitLab Handbook (People Group and Total Rewards)

A real, public department corpus: 94 HR pages (leave, benefits by country, onboarding, offboarding, promotions, learning, compensation) from the [GitLab Handbook](https://gitlab.com/gitlab-com/content-sites/handbook) at commit `67bc662`. The content is fetched to `/tmp`, processed locally and never committed here; only the questions are.

```bash
samples/gitlab-handbook/fetch.sh /tmp/gl-handbook
okf-ingest --data-dir data/gitlab build /tmp/gl-handbook/content/handbook
okf-ingest --data-dir data/gitlab eval samples/gitlab-handbook/questions.yaml
```

The 33 questions (30 answerable, 3 with no answer) were written from the page titles only, phrased as an employee would ask, before reading the pages. Results on 24 September 2026 (2,069 passages):

| Search | Hit@1 | Hit@5 | MRR | All evidence |
| --- | --- | --- | --- | --- |
| Keyword only | 0.60 | 0.80 | 0.67 | 0.63 |
| Hybrid (keyword + semantic) | 0.73 | 0.93 | 0.83 | 0.83 |

The two remaining misses are arguably expectation gaps ("paid time off to volunteer" ranks *Time Off Types* first). Questions with no answer still return loosely related pages; the assistant must judge relevance.

This is a real corpus, but the questions are still the operator's. The decisive pilot uses your own department's documents and questions collected from its users.
