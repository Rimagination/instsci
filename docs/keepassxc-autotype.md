# KeePassXC Auto-Type Assist

InstSci can help with KeePassXC Auto-Type without seeing institution
credentials. KeePassXC owns the username and password; InstSci only helps users
reach the visible institution login page, show non-secret setup hints, focus the
username field, and optionally trigger the configured global Auto-Type hotkey.

InstSci must not read KeePassXC entries, export passwords, inspect password
fields, use the clipboard for credentials, ask users to paste passwords into
chat or terminal, or click the final login/submit button for a credential form.
The user clicks login and completes SMS, TOTP, push approval, CAPTCHA, recovery
prompts, and final confirmation in the visible CloakBrowser window.

This workflow is institution-neutral. It is for users from universities, labs,
hospitals, companies, libraries, and other subscription organizations. Examples
such as Tsinghua are configuration examples, not defaults.

## First-Time Setup

Use this checklist for users who have never configured KeePassXC Auto-Type.

1. Run the normal InstSci publisher workflow until CloakBrowser reaches the
   user's institution login page.
2. Read only two non-secret hints from the visible login page or
   `human_assist/assist_state.json`:
   - `expected_domain`: the address-bar hostname, such as `idp.example.edu`
   - `window_association_hint`: a title pattern such as
     `*Example University Login*`
3. Install KeePassXC from the official package or a trusted OS package source.
4. Create or open a KeePassXC database and protect it with a strong master
   password. Keep backups of the `.kdbx` file, but never export plaintext
   passwords.
5. Create one entry for the institution identity-provider login, not one entry
   per publisher:
   - `Title`: `Institution SSO - <Institution Name>`
   - `Username`: the user's institution account, student id, employee id, or
     library account name
   - `Password`: the user's institution password
   - `URL`: `https://<expected_domain>/`, for example
     `https://idp.example.edu/`
6. Configure entry-level Auto-Type. In the KeePassXC Chinese UI:

   ```text
   选中机构登录条目 -> 条目 -> 编辑条目 -> 左侧 自动输入
   ```

   On that page:
   - check `为此条目启用自动输入`
   - choose `使用自定义自动输入序列`
   - set the sequence to:

     ```text
     {USERNAME}{TAB}{PASSWORD}
     ```

7. Add a window association on the same entry-level Auto-Type page.
   - Click `+` under `窗口关联`.
   - Put the human-assist hint into the `窗口标题` field, for example:

     ```text
     *Example University Login*
     ```

   - For the Tsinghua IdP page observed through CloakBrowser, the useful pattern
     was:

     ```text
     *清华大学用户电子身份服务系统*
     ```

   The leading and trailing `*` are intentional. They let KeePassXC match the
   login page even when the browser appends text such as `- Chromium`.
8. Configure KeePassXC's global Auto-Type hotkey. In the Chinese UI:

   ```text
   齿轮设置 -> 常规 -> 自动输入
   ```

   This is the top tab named `自动输入` next to `基础设置` inside the `常规`
   settings page. It is not the left-sidebar `浏览器集成` page; browser
   integration is for the KeePassXC-Browser extension and is not required for
   CloakBrowser Auto-Type.

   The field `全局自动输入快捷键` must show a real shortcut. If it is blank,
   KeePassXC has not registered a global Auto-Type hotkey and InstSci cannot
   trigger it. InstSci defaults to `Ctrl+Alt+A`; use the same shortcut in
   KeePassXC or pass another shortcut with `--hotkey`.
9. Optional: if the window association works and the user trusts this local
   workflow, uncheck `总在执行自动输入前询问`. This removes the extra KeePassXC
   confirmation popup, but does not submit the login form because the
   recommended sequence does not include `{ENTER}`.
10. Save the KeePassXC database. Keep it locked when not in use; unlock it only
    when an institution login is expected.

## What Users Store

Users store their own institution account in their own local KeePassXC database.
InstSci never receives the username/password pair.

The entry URL should be the school or organization login page reached after the
publisher redirects through Shibboleth, OpenAthens, CARSI, or another federation
broker. It is usually not a publisher site such as `sciencedirect.com`,
`ieeexplore.ieee.org`, `nature.com`, or `wiley.com`.

If the user does not know the IdP host, run the normal InstSci browser workflow
and pause at the visible institution login page. Use only the hostname from the
address bar. Do not copy full redirected SSO URLs into tickets, docs, commits,
or chat because they may contain transient login tokens.

## CLI

Use the command first in guidance mode:

```powershell
instsci keepassxc-autotype `
  --expected-domain idp.example.edu `
  --login-url https://idp.example.edu/login
```

After CloakBrowser is on the expected login page and the username field is
focused, trigger the hotkey:

```powershell
instsci keepassxc-autotype `
  --expected-domain idp.example.edu `
  --trigger
```

The trigger path asks for confirmation, waits for the configured countdown, and
then sends the hotkey to the currently focused window. It does not retrieve or
print credential values.

For a first-time setup where the exact host may be `idp.example.edu` or
`login.example.edu`, use the parent institution domain only after visually
confirming the address bar belongs to the intended organization:

```powershell
instsci keepassxc-autotype `
  --expected-domain example.edu `
  --trigger
```

Prefer exact IdP hosts once they are known.

## Human-Assist Integration

When `--human-assist` is enabled and a publisher run reaches an institution
login, MFA, CAPTCHA, or re-authentication checkpoint, InstSci includes a
non-secret `credential_assist` object in `human_assist/assist_state.json` and
`/status.json`. The local human-assist page renders the same information as a
KeePassXC Auto-Type section.

The object is intentionally small:

```json
{
  "provider": "keepassxc",
  "mode": "auto_type",
  "expected_domain": "idp.example.edu",
  "window_association_hint": "*Example University Login*",
  "trigger_command": "instsci keepassxc-autotype --expected-domain idp.example.edu --trigger",
  "setup_doc": "docs/keepassxc-autotype.md",
  "first_time_setup_steps": [
    "Create one KeePassXC entry for the institution IdP host shown here.",
    "Set the URL to https://idp.example.edu/.",
    "Enable entry-level Auto-Type and use {USERNAME}{TAB}{PASSWORD}.",
    "Add the window association hint.",
    "Set KeePassXC's global Auto-Type hotkey."
  ]
}
```

Only the hostname is copied from a visible login URL. Full redirected SSO URLs
are not stored in this field. When a broker is already paused for
re-authentication and the exact IdP host is not known, the command uses
`<institution-idp-host>` so the user or operator can fill in the visible
address-bar host.

During a publisher test, no KeePassXC trigger is needed if the publisher already
has an authorized browser session and the PDF is captured directly. The
Auto-Type handoff is only for visible institution login pages; do not trigger it
on publisher article pages, Cloudflare pages, or PDF viewers.

## Troubleshooting

If InstSci sends the Auto-Type hotkey but nothing appears in the login form,
check these non-secret settings:

1. KeePassXC `齿轮设置 -> 常规 -> 自动输入`: `全局自动输入快捷键` is not blank.
2. The global shortcut matches the command InstSci is sending. The default is
   `Ctrl+Alt+A`.
3. The institution entry has `为此条目启用自动输入` checked.
4. The entry has a window association matching the visible CloakBrowser title,
   such as `*Example University Login*`.
5. The entry sequence is `{USERNAME}{TAB}{PASSWORD}` unless the institution
   uses a different form layout.
6. The CloakBrowser username field is focused before the hotkey is sent.
7. If a KeePassXC `自动输入` confirmation window appears, either confirm it
   manually or, after verifying the association is correct, uncheck
   `总在执行自动输入前询问`.

If pressing the hotkey manually works but the InstSci trigger does not, keep the
KeePassXC setup as-is and complete that login with the manual hotkey. That means
the password-manager configuration is correct, and the remaining issue is
Windows accepting or rejecting synthetic global-hotkey events from the helper
process.

Some institutions split username and password across two pages. For those, use
separate manual steps or an institution-specific sequence such as
`{USERNAME}{ENTER}`, then type the password on the second page with KeePassXC's
entry-level "type password" option. Do not make `{ENTER}` the default for a
shared workflow.

## Operator Playbook

When helping a user from a new institution:

1. Ask for the institution name only, not the password.
2. Run the normal publisher workflow with `--institution "<Institution Name>"`.
3. When CloakBrowser pauses on SSO, MFA, CAPTCHA, or a federation page, ask the
   user to verify the visible address-bar host.
4. Point the user to the first-time checklist above and the non-secret
   `expected_domain` / `window_association_hint` in the human-assist page.
5. Trigger Auto-Type only after the user confirms KeePassXC is unlocked and the
   username field is focused.
6. Let the user click login and complete SMS codes, authenticator approvals,
   CAPTCHA, recovery prompts, and final login confirmation in CloakBrowser.

Do not ask the user to paste credentials into chat, terminal, config files, bug
reports, or docs. If a screenshot is needed for support, it should hide password
fields, SMS codes, recovery codes, and personal recovery information.

## Safety Notes

- Domain checks are only a guardrail for URLs passed to the command. Always
  visually verify the real browser address bar before triggering Auto-Type.
- Auto-Type sends keystrokes to the focused window. If the wrong window is
  focused, KeePassXC may type into the wrong place.
- Prefer exact IdP hostnames in KeePassXC URLs and window associations.
- Do not use command-line password exports such as `bw get password`,
  `op item get`, `.env` files, or clipboard-based credential transfer.
- Keep `{USERNAME}{TAB}{PASSWORD}` as the shared default. Users click login
  themselves.
