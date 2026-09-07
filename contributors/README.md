# Contributor email mappings

Contributor mappings associate commit-author email addresses with GitHub logins for release attribution.

## Adding a mapping

Use `scripts/add_contributor.py` with the exact commit-author email and GitHub login.
The helper creates a file under `emails/` and refuses conflicting mappings.

Each filename is an exact commit-author email.
The first non-comment line contains its GitHub login.
Lines beginning with `#` contain optional notes.

GitHub noreply addresses can resolve directly from their embedded login.
The attribution check reports emails that need a mapping.

## Case-sensitive email aliases

Store case-colliding mappings in `email-aliases.json` as exact-email keys with GitHub login values.
Keep the corresponding portable filename in `emails/`.
The release generator and attribution checks include both sources.
The contributor helper uses aliases before checking filenames and refuses new case-colliding filenames.

For example, `agent@Agents-Mac-mini.local` maps to `skip-agent` in the alias file.
The file `emails/agent@agents-Mac-mini.local` maps the lowercase email to `momomojo`.
Both attributions remain distinct on case-insensitive filesystems.

## Existing mappings

Keep `LEGACY_AUTHOR_MAP` in `scripts/release.py` frozen.
Use contributor files or aliases for new mappings.
