# studio_lint: catch Rasa Studio upload errors before you upload

`studio_lint.py` finds the errors that make `rasa studio upload` fail, without connecting to Studio. These are errors that `rasa train` and `rasa data validate` don't catch.

You get three files: `studio_lint.py`, `requirements.txt` and this README. Run the script from your Rasa project root. The examples assume `studio_lint.py` is in that folder; if you keep it elsewhere, use its path instead, for example `python tools/studio_lint.py`.

- **Built for:** Rasa Studio **1.17.x**. The checks match Studio's upload validation, which is the same in every release from 1.17.0 to 1.17.7.
- **Needs:** `rasa-pro` installed, on any Python version your `rasa-pro` supports (3.10–3.13 for Rasa Pro 3.18; tested on 3.12). The easiest option is the same environment you run `rasa studio upload` in. The script uses Rasa's own importer, so it sees your project exactly as `rasa studio upload` does. In a fresh virtualenv, run `pip install -r requirements.txt`, after setting `rasa-pro` in that file to the version you upload with. A run takes a few seconds.
- **Rasa Pro versions:** 3.18 to 3.21. It was tested on 3.18.10. For 3.19 to 3.21, the Rasa code the script relies on and the upload payload were checked and are unchanged, but the script hasn't been run on those versions yet.
- **Changes your project?** No, it only reads it.

## Run it

From the project root:

```bash
python studio_lint.py
```

It reads `data/`, `domain.yml` (or `domain/`), `config.yml` and `endpoints.yml`. To point it at other paths:

```bash
python studio_lint.py -d data --domain domain --config config.yml --endpoints endpoints.yml
```

Run `rasa data validate --fail-on-warnings` as well. The lint only adds Studio's rules on top of Rasa's own checks.

To save the findings to files, add either or both of these options:

```bash
python studio_lint.py --report studio-lint-report.txt --json studio-lint.json
```

| Option | Writes | For |
| --- | --- | --- |
| `--report PATH` | A readable report. The result and counts come first, then errors and warnings grouped by file area and rule, each with a "How to fix" line. | People |
| `--json PATH` | Every finding with `severity`, `rule`, `area`, `location`, `file`, `line`, `message` and `fix`, plus `result` (`pass`, `fail` or `load_error`), `exit_code` and counts per rule | CI and other tooling |

Both files are written even when the lint fails, or when the project can't be read (`result: load_error`). In GitLab, the `studio-lint` job saves them as artifacts. Findings are always printed to the terminal as well.

## Read the output

In the terminal, each problem is printed on one line, and a summary line comes last:

```text
ERROR   [SLOT-TYPE] domain.yml:3584  domain:slots.recent_payees: type 'list' is not supported by Studio; use one of [...]
ERROR   [COND-INVALID_CONDITION] data/flows.yml:33  flows:accounts_close.if: Parentheses are not allowed in conditions: ...
ERROR   [STEP-UNSUPPORTED] data/flows.yml:71  flows:accounts_open_current.steps[collect is_individual_account]: Multiple rejections ...
WARNING [DOM-DROPPED] domain.yml:3588  domain: these top-level keys are NOT sent to Studio and will be lost: [...]

studio_lint: 3 error(s), 1 warning(s)
```

Each line shows:

- **`file:line`:** where to fix it. In VS Code's terminal you can Ctrl/Cmd-click it to jump there. For flow steps this is the step's first line. For branch conditions (`next: - if:`) it's the step that owns the branch.
- **What it is:** the slot, response (with its variation number, counting from 0), flow, or step. Steps without an `id` are named by what they do, such as `collect is_individual_account` or `action utter_x`.
- **What's wrong:** the problem, quoting the condition or the bad `{...}` text where that helps.

The `--json` file includes `file` and `line` for each finding.

- **ERROR:** Studio will reject the upload, or import a broken assistant (STEP-EMPTY-MESSAGE). Fix every one.
- **WARNING:** The upload works, but something is silently dropped or changed. Review them.

| Exit code | Meaning |
| --- | --- |
| 0 | No errors |
| 1 | Errors found (warnings count too with `--fail-on-warnings`) |
| 2 | Rasa couldn't load the project, for example a missing file or invalid YAML. Fix the `[LOAD]` error and run the lint again. **No Studio checks ran.** |

## Know its limits

- **It mirrors Studio's checks but isn't Studio.** If an upload still fails after a clean lint, send the Studio error message to Lena so the missing rule can be added. Alternatively, add them yourself.
- **Studio's built-in defaults may differ slightly.** Studio checks references against its own default patterns, slots and actions. The lint uses the installed Rasa Pro's defaults, which are very close but not identical. Newer Rasa Pro versions may add built-ins that Studio 1.17 doesn't know about, so a reference to one could pass the lint and still fail in Studio.
- **Newer Rasa Pro fields aren't flagged.** Rasa Pro 3.19 to 3.21 added optional fields: `on_activate` and `metadata` on flows, `collect-tool-binding` on collect steps, and `allow_llm_set` on slots. Studio 1.17 most likely drops them silently on upload, and the lint doesn't warn about them.
- **`context.*` conditions aren't checked.** The list of valid contexts lives inside Studio.
- **Problems on a broken step can show up one at a time.** If a step doesn't have exactly one of `collect`, `action`, `link`, `set_slots`, `call` or `noop`, its other checks are skipped until you fix it.
- **Re-check after a Studio upgrade beyond 1.17.x.** The rules may change.

## Most common errors and how to fix them

| Rule | Problem | Fix |
| --- | --- | --- |
| COND-* | A condition Studio can't import: parentheses, mixing `and` with `or`, `slots.a>1` without spaces, comparing two slots, or `is null` on a bool slot | Rewrite it in the allowed form (see below), or split it into several conditions |
| RESP-FORMAT | `{...}` in a response that holds something other than a slot name, such as `{ name }`, `{user.name}` or a stray `{` | Use `{slot_name}`, or `{{ }}` for Jinja |
| RESP-SLOT-REF | `{slot}` names a slot that doesn't exist | Add the slot or fix the name |
| SLOT-TYPE | A slot of type `list` or a custom slot class | Use `text`, `bool`, `categorical`, `float` or `any` |
| SLOT-MAPPING | A mapping of type `from_trigger_intent`, a condition other than `active_flow`, or a reference to something unknown | Use a supported mapping type; fix the reference |
| STEP-UNSUPPORTED | More than one entry under `rejections` in a collect step | Combine them into one rejection |
| STEP-REF | A collect, set_slots, action, link or call target that doesn't exist. `call` can only target flows. | Fix the name or add the missing item |
| STEP-EMPTY-MESSAGE | `action: utter_x` where `utter_x` doesn't exist. Studio doesn't reject this, but the flow gets an empty message, so it's reported as an error. | Add the response or fix the name |
| FLOW-PERSISTED | A slot in `persisted_slots` that is also collected with `reset_after_flow_ends: false` | Use only one of the two |
| *-TRANSLATION | A translation in a language that isn't in `additional_languages` | Add the language to `config.yml` or remove the translation |
| RESP-NAME, SLOT-NAME, ACTION-NAME, INTENT-NAME, ENTITY-NAME | A name with spaces, `+`, `/` or `!`, a response that doesn't start with `utter_`, or a slot name over 128 characters | Rename it |

### How to write conditions that Studio accepts

| Works | Fails |
| --- | --- |
| `slots.age > 18` | `slots.age>18` |
| `slots.a and slots.b` | `slots.a and slots.b or slots.c` |
| `not slots.confirmed` | `(slots.a or slots.b)` |
| `slots.tier = "gold"`, when `gold` is one of the slot's values | `slots.start < slots.end` |
| `slots.name is null` on a text, categorical or float slot | `slots.flag is null` on a bool slot |
| `{"a" "b"} contains slots.x` | `not slots.age > 3` |

Bool slots accept only `is` / `is not` / `=` / `!=` compared with `true` or `false`.

### Warnings

| Rule | Meaning |
| --- | --- |
| DOM-DROPPED | Top-level domain keys that the upload doesn't send to Studio |
| NLU-DROPPED | NLU intents not declared in the domain; Studio ignores them |

## Using it in CI or a pre-commit hook

The script works as a normal command-line check. Install `requirements.txt`, then run it from the project root:

- **Pass or fail:** use the exit code (0 pass, 1 errors, 2 not checked).
- **Reports:** add `--report` and `--json` to keep the results, for example as CI artifacts. Both files are written even when the check fails.
- **Rasa license:** `rasa-pro` needs its license, usually via `RASA_PRO_LICENSE`, wherever it runs.
- **Nothing is uploaded to Studio**, so no Studio access or credentials are needed.
