# Repository CI policy and merge evidence

Kanban records worker verification, independent review, GitHub CI and merge authority separately. Publication of a branch or PR can finish with a visible CI decision outstanding. A promised merge requires a retained successful preflight for the same repository, PR, head, target branch and policy. Completion also checks the resulting merged publication and current evidence.

## Approved policy

Record each repository's approved policy in the selected profile's `config.yaml`, under `kanban.repository_ci_policies`. Repository keys use lower-case `owner/repository` names. Target branch keys are exact and case-sensitive. The default map is empty. Missing or malformed policy produces a decision point and never implies permission to merge without CI.

Each branch entry has these fields:

| Field | Meaning |
|---|---|
| `approval` | Non-empty reference to the user's approval of this policy |
| `branch_rules` | Non-empty record of how this policy relates to the target's reviewed branch rules |
| `required_checks` | List of required check identities |
| `allow_no_ci` | Explicit boolean. An empty requirement list requires `true` |

Each required check contains `kind` (`check_run` or `status`) and its exact `name`. A check run can specify an integer `app_id` to require its emitting GitHub App. An omitted application ID matches all reported applications with that name. Every matching check run must satisfy the requirement. Duplicate names cannot hide a failure. Commit statuses use the latest status ID for each context.

The optional boolean `allow_skipped` defaults to false for each requirement. A successful result satisfies a requirement. A skipped check satisfies it only when this flag is explicitly approved. Pending, failed, absent and unavailable results produce decisions. Neutral and unknown conclusions remain unavailable for satisfaction, with their raw conclusions retained.

The following is a fixture policy example, not an approval for a real repository:

```yaml
kanban:
  repository_ci_policies:
    fixture/repository:
      main:
        approval: Fixture approval
        branch_rules: Additional floor to fixture branch rules
        required_checks:
          - kind: check_run
            name: unit
            app_id: 42
            allow_skipped: false
        allow_no_ci: false
      documentation:
        approval: Fixture approval for documentation
        branch_rules: Fixture branch permits merging without CI
        required_checks: []
        allow_no_ci: true
```

This policy supplements GitHub enforcement. Review branch protection and rulesets when approving or changing it. The receipt stores the approved relationship and live PR merge state, not a complete export of branch rules. A clean or unstable merge state still needs the configured checks to satisfy policy. Other merge states and draft PRs require a decision. A local policy or CI exception cannot bypass GitHub branch enforcement. Changing branch rules requires its own authority.

## Publication and merge handoff

Record developer verification in the review request's `tests_run` metadata. Existing non-empty reports and positive test counts are retained as reported evidence, with the developer run and submitted head. A report is not an independently established test pass. Missing evidence and reports from another head remain explicit. The independent review must cover the exact retained state, including HEAD and index, before a merge.

The operator records the user's task-specific merge approval through the publication authorisation operation. Its scope includes the observed repository, PR, head and target branch. Retargeting a PR or changing its head requires reconciliation and appropriate fresh authority. Merge-only dispatch waits while its recorded decision remains unresolved. Publication actions needed to create a branch or PR can proceed within their own authority.

Use the Kanban CLI's `merge-check` operation with the card ID immediately before an authorised merge. It reads the live PR twice, queries check runs and statuses at the PR head, and persists its decision in `publication.merge_preflight`, `publication.merge_evidence` and a `merge_preflight` event. Exit status zero means the recorded preflight is ready. A non-zero result needs a decision or fresh evidence. The CLI and worker task context expose the complete receipt. Browser and desktop task data retain the same publication record.

The caller must use the returned head as the GitHub CLI's `--match-head-commit` value for the separately authorised merge. Preserve normal branch enforcement and avoid administrative bypass or unattended auto-merge. A changed head must trigger a fresh preflight, not an unconditional retry. A preflight does not execute a merge or intercept arbitrary shell commands. Head matching does not lock the target branch, checks or repository policy after the observation. Reconcile these again after an interrupted or delayed handoff.

The authoritative CI receipt comes from bounded, paginated check-run and commit-status reads. It records repository, PR, commit SHA, target branch and observation time. An empty successful query reports `absent`. Failed, malformed, incomplete or inconsistent queries report `unavailable`. Pagination is limited to 20 pages per endpoint, with a shared 30-second budget for both check endpoints. A partial result cannot establish passing CI. The informational PR rollup remains available separately. Workflows that report only against a synthetic merge commit require an explicit policy decision when head checks are absent.

## Scoped exceptions

An operator can supply an already user-approved exception through `--exception-json` on `merge-check`. Workers can refresh their own task's preflight but cannot record an exception. The operation records approval, it does not obtain approval on the user's behalf.

Supply the exact `repository`, `pr`, `head` and `target_branch` from the decision's `scope`, its `policy.digest` as `policy_digest`, its `ci_digest`, a non-empty `approval` reference, a non-empty `reason` and an `issues` list containing only the expressly waived check issues. Required-check issue identifiers and `ci_unavailable` can be waived. Missing policy, worker evidence, independent review, merge authority, PR identity and GitHub branch enforcement cannot be waived here.

The exception retains the failed, skipped or unavailable CI result. It does not relabel CI as successful. A changed repository, PR, head, target, policy or check result invalidates it. Supplying an empty JSON object clears the active exception. Earlier decisions remain in the event history. An interrupted merge can retain its prior preflight while the operator reconciles consumed actions and authorises verification.

GitHub documents [check runs](https://docs.github.com/en/rest/checks/runs#list-check-runs-for-a-git-reference), [commit statuses](https://docs.github.com/en/rest/commits/statuses#list-commit-statuses-for-a-reference), [conditional PR merging](https://cli.github.com/manual/gh_pr_merge) and [protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
