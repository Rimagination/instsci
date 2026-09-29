# Publisher Supplementary Materials Smoke Matrix

Source of truth: InstSci built-in visible CloakBrowser workflow. HTTP-only probes are not final supplementary capability verdicts.

Generated: 2026-06-23
Mode: public/OA smoke test, no configured subscription institution
CLI/implementation mode: supplementary downloads are opt-in through `--include-supplementary` / `include_supplementary=True`.
Optimization update: on 2026-06-28, five Science Advances supplementary PDFs were captured and PDF-validated in
`runs/science_supplementary_direct_batch5_20260628_224455/summary.json`. The lesson from that slow manual batch is
to use a supplementary-only fast path for Science attachments: skip primary PDF capture, collect article-page
supplementary candidates, add the predictable `/doi/suppl/<doi>/suppl_file/<doi-suffix>_sm.pdf` candidate for
`10.1126/sciadv.*`, and cap supplementary click waits separately from primary PDF waits.
AAAS scope correction: on 2026-07-01, a broader AAAS partial smoke test showed that `Science Advances` is not a
complete proxy for all Science/AAAS journals. The downloader was expanded to generate standard and short-suffix
`_sm.pdf` candidates for `science.*`, `sciadv.*`, `scitranslmed.*`, `scisignal.*`, `sciimmunol.*`, and
`scirobotics.*`, and to skip duplicate final downloads. Browser evidence so far: `Science Advances` and
`Science Immunology` saved valid files; `Science Translational Medicine` and `Science Signaling` hit AAAS/SeamlessAccess
login before user completion; `Science Robotics` was not reached in that partial run; one sampled `Science` main-journal
DOI did not yield a supplementary file from the standard/short candidates.
AAAS broker restart: later on 2026-07-01, after the user completed institution login in the visible long-lived
`science` broker, a clean article-page supplementary-only run succeeded for five AAAS journals with 11 captured
attachments. The important workflow fix was to avoid `--carsi-portal-preauth` for this route and avoid manual
navigation to CARSI resource pages or arbitrary `ssostart` URLs. Let the broker open Science article pages, let
Science/Atypon/OpenAthens route to the user-selected institution, verify visible article-page access markers, then
submit or resume supplementary jobs in the same broker context.

## Summary

The opt-in supplementary downloader worked for some publishers, but the 2026-06-23 cross-publisher smoke test did not show universal supplementary download support.

Clean saved-file evidence:

- `copernicus`: captured a supplementary PDF for `10.5194/acp-24-1-2024`.
- `science`: captured a Science supplementary PDF for `10.1126/sciadv.adp3964` after clicking the article-page `DOWNLOAD` control.
- `springer`: captured a Nature/Springer supplementary PDF for `10.1038/s41586-020-2649-2`.
- `ams`: captured a supplementary PDF for `10.1175/aies-d-23-0093.1` in the first long run.

Important caveats:

- `plos` saved PNG/TIF files in the first long run, but the current marker set can match article figure/image assets. Do not count this as clean supplementary success until PLOS markers are narrowed to true supplementary files.
- `rsc` visibly exposed a `Supplementary information PDF (4128K)` link for `10.1039/d5cp03829d`, including a direct candidate URL, but the downloader saved no supplementary file. This is the highest-priority repair.
- Challenge, timeout, or authentication blockers remain browser-verified blockers for that smoke run, not publisher-wide unsupported verdicts.

## Publisher Results

| Publisher | DOI | Result | Supplementary candidates | Downloaded | Evidence | Next action |
|---|---|---:|---:|---:|---|---|
| acm | `10.1145/3448016.3452834` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Retest with a longer visible broker run and institution state if required. |
| acs | `10.1021/acs.est.6c00693` | blocked | 0 | 0 | `Page.goto timeout 20000ms` | Retest with publisher broker and user-selected institution if the article is closed-access. |
| aip | `10.1063/5.0237567` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Retest in visible CloakBrowser; do not mark unsupported from this smoke. |
| ams | `10.1175/aies-d-23-0093.1` | browser verified | 3 | 1 | `runs/supplementary_smoke_20260623_230336/ams/supplementary/10.1175_aies-d-23-0093.1/10.1175_aies-d-23-0093.1_supplementary_03.pdf` | Make candidate handling deterministic; the shorter second run found candidates but saved none. |
| annual-reviews | `10.1146/annurev-phyto-011325-012824` | blocked | 2 | 0 | Candidates found, no file captured. | Follow landing/action links to real attachment URLs. |
| aps | `10.1103/PhysRevLett.128.161102` | auth_required | 0 | 0 | Article page showed login/access controls before supplementary links were visible. | Requires user subscription institution before closed-access retry. |
| copernicus | `10.5194/acp-24-1-2024` | browser verified | 2 | 1 | `runs/supplementary_smoke3_20260623_231800/copernicus/supplementary/10.5194_acp-24-1-2024/10.5194_acp-24-1-2024_supplementary_01.pdf` | Keep as a regression sample. |
| elsevier | `10.1016/j.watres.2024.121507` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Needs entitlement/API or visible broker repair; no supplement verdict from this smoke. |
| frontiers | `10.3389/fmicb.2026.1831710` | blocked | 4 | 0 | Candidates found, no file captured. | Follow candidate links and inspect content type before saving. |
| ieee | `10.1109/jstqe.2026.3687110` | browser verified | 0 | 0 | Sample page produced no supplementary candidate. | Use a known IEEE article with supplementary files before judging support. |
| iop | `10.1088/1361-648x/ae72dd` | browser verified | 0 | 0 | Sample page produced no supplementary candidate. | Use a known IOP article with supplementary files before judging support. |
| mdpi | `10.3390/foods10081757` | browser verified | 0 | 0 | Short run found no candidates; first run once saw 4 candidates but saved none. | Investigate lazy loading/scrolling and profile markers. |
| oxfordacademic | `10.1093/nar/gkaa892` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Retest with longer visible run. |
| plos | `10.1371/journal.pone.0000001` | blocked | 0 | 12 caveated | Short run timed out; first long run saved PNG/TIF image assets. | Narrow PLOS supplementary markers so `figure/image` does not capture ordinary figures. |
| pnas | `10.1073/pnas.2309123120` | blocked | 3 | 0 | Candidates found, no file captured. | Follow landing/action links to real files. |
| royalsocietypublishing | `10.1098/rsos.150470` | blocked | 0 | 0 | `Page.goto timeout 20000ms` | Retest with longer visible run. |
| rsc | `10.1039/d5cp03829d` | blocked | 1 | 0 | Visible page showed `Supplementary files`; candidate `https://www.rsc.org/suppdata/d5/cp/d5cp03829d/d5cp03829d1.pdf`. | Priority bugfix: direct RSC suppdata PDF should save. |
| science | `10.1126/sciadv.adp3964` | browser verified | 2 | 1 | `runs/science_supplementary_formal_retry4_20260624_002930/primary/supplementary/10.1126_sciadv.adp3964/10.1126_sciadv.adp3964_supplementary_01.pdf` | Keep as a regression sample; direct `/doi/suppl/...pdf` may 403, so click the article-page `DOWNLOAD` link in the visible browser and use `suggested_filename` for the `.pdf` extension. |
| springer | `10.1038/s41586-020-2649-2` | browser verified | 2 | 1 | `runs/supplementary_smoke3_20260623_231800/springer/supplementary/10.1038_s41586-020-2649-2/10.1038_s41586-020-2649-2_supplementary_01.pdf`; screenshot showed `Supplementary information`. | Keep as a regression sample. |
| wiley | `10.1002/adfm.202525261` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Retest in visible broker; likely needs longer challenge handling. |
| world-scientific | `10.1142/s0218194026500348` | blocked | 0 | 0 | `challenge_or_viewer_timeout` | Retest in visible broker. |

## Engineering Notes

- The normal paper/PDF path should continue filtering supplementary URLs by default. Only opt in when the user asks for appendices, supporting information, or supplementary materials.
- Store captured files separately from primary PDFs. The current layout copies them to `complete/supplementary/<doi>/` and records `supplementary_count` plus `supplementary_paths`.
- Prefer direct binary/file assets when the visible article page exposes them, but keep the route within the same CloakBrowser-backed state so publisher cookies, challenges, and generated tokens are respected.
- Validate saved files by header/content type and size. Do not let HTML landing pages, one-page previews, or ordinary article figures become supplementary successes.
- For RSC, add a regression around `https://www.rsc.org/suppdata/.../*.pdf` because the visible page and candidate URL were good but no file was saved.
- For Science, skip `#supplementary-materials` page anchors as navigation-only candidates. Direct supplementary PDF URLs can return 403/Cloudflare, while the visible article-page `DOWNLOAD` control produces a browser download from `action/downloadSupplement`; prefer normal Playwright clicks and derive the file extension from `download.suggested_filename`.
- For Science/AAAS batch attachment runs, prefer `instsci publisher-batch dois.txt --publisher science --supplementary-only --supplementary-fast --output ./runs/science_supplementary`. This avoids the main-PDF capture path, but do not claim all AAAS journals are covered until each sub-journal has browser-verified evidence.
- For the broker-backed Science/AAAS attachment route, do not use `--carsi-portal-preauth` unless the task is explicitly CARSI-portal testing. The successful route is article page -> Science login/access control -> user-selected institution SSO -> return to article page with `FULL ACCESS` / `brought to you by <institution>` / `Supplementary Materials` visible -> browser downloads from `action/downloadSupplement`.
- If the browser lands on `ds.carsi.edu.cn`, a hand-written `action/ssostart` URL, or a same-page `#main-content-focus` skip anchor, treat that as a confused navigation path and enqueue a clean article-page broker job rather than continuing to edit the address bar.
- For AAAS, generate both `<doi-suffix>_sm.pdf` and short `<article-id>_sm.pdf` candidates. Examples: `sciadv.abd6034_sm.pdf` and `abd6034_sm.pdf`.
- For AAAS multi-file articles, skip duplicate final downloads by final URL/content hash. The 2026-07-01 Immunology smoke run saved the same PDF twice before this was fixed.
- For PLOS, remove or narrow broad `figure/image` matching; it captured image assets that are not necessarily supplementary files.

## Science/AAAS Partial Smoke, 2026-07-01

Run: `runs/aaas_supplementary_smoke_20260701_001612`

| Journal | DOI | Result | Downloaded | Evidence | Next action |
|---|---|---:|---:|---|---|
| Science | `10.1126/science.abe5017` | inconclusive | 0 | `supplementary_not_captured`; tried standard and short `_sm.pdf` candidates. | Retest with a known main-journal article that visibly has supplementary materials. |
| Science Advances | `10.1126/sciadv.abd6034` | browser verified | 2 | PDF 1,656,215 bytes / 82 pages; XLSX 57,149 bytes zip-valid. | Keep as a non-`adp3964` regression sample. |
| Science Translational Medicine | `10.1126/scitranslmed.adv0702` | auth_required | 0 | AAAS/SeamlessAccess login was not completed before timeout. | Retry after user completes institution login in visible CloakBrowser. |
| Science Signaling | `10.1126/scisignal.adr3411` | auth_required | 0 | AAAS/SeamlessAccess login was not completed before timeout. | Retry after user completes institution login in visible CloakBrowser. |
| Science Immunology | `10.1126/sciimmunol.abg5021` | browser verified | 4 caveated | PDF 2,868,636 bytes / 34 pages; two `.pyz` files zip-valid; duplicate PDF saved before dedupe fix. | Keep as multi-file regression; rerun after dedupe fix for clean counts. |
| Science Robotics | `10.1126/scirobotics.adi9754` | not_reached | 0 | Run was stopped after Immunology duplicate-download issue was found. | Retry after user completes AAAS login. |

## Science/AAAS Broker Restart, 2026-07-01

Run: `runs/aaas_supplementary_restart_20260701_112424`

Command shape: `python -m instsci.cli publisher-batch <dois.txt> -p science --institution "Institution Name" --supplementary-only --supplementary-fast --broker --broker-ttl 259200 --detach --output <run>`

Result: 5 records, 5 successes, 11 supplementary attachments, `broker=true`, `carsi_portal_preauth=false`.

| Journal | DOI | Result | Downloaded | Evidence | Workflow note |
|---|---|---:|---:|---|---|
| Science | `10.1126/science.adz3996` | browser verified | 1 | `runs/aaas_supplementary_restart_20260701_112424/complete/manifest.csv` | First record triggered user institution login, then returned to the article page before downloading. |
| Science Translational Medicine | `10.1126/scitranslmed.adv0702` | browser verified | 3 | `runs/aaas_supplementary_restart_20260701_112424/complete/supplementary/10.1126_scitranslmed.adv0702/` | Reused the same live broker login state after the first article. |
| Science Signaling | `10.1126/scisignal.adr3411` | browser verified | 2 | `runs/aaas_supplementary_restart_20260701_112424/complete/supplementary/10.1126_scisignal.adr3411/` | Reused the article-page supplementary route. |
| Science Immunology | `10.1126/sciimmunol.abg5021` | browser verified | 3 | `runs/aaas_supplementary_restart_20260701_112424/complete/supplementary/10.1126_sciimmunol.abg5021/` | Multi-file attachments include PDF and zip-like downloaded assets. |
| Science Robotics | `10.1126/scirobotics.adi9754` | browser verified | 2 | `runs/aaas_supplementary_restart_20260701_112424/complete/supplementary/10.1126_scirobotics.adi9754/` | Confirms this AAAS sub-journal works after broker login is established. |
