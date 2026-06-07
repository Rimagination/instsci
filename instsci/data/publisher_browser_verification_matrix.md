# Publisher Browser PDF Verification Matrix

Source of truth: InstSci built-in CloakBrowser workflow. HTTP preflight reports are not final capability verdicts.

Generated: 2026-06-07
Verified: 19/21 publishers
Pending: 1; Unsupported: 1

| Publisher | Verdict | DOI | Route kind | Candidate / final URL | Notes |
|---|---:|---|---|---|---|
| acm | verified | 10.1145/3448016.3452834 | doi_pdf | https://dl.acm.org/doi/pdf/10.1145/3448016.3452834 | PDF captured |
| acs | verified | 10.1021/acs.est.6c00693 | doi_pdf | https://pubs.acs.org/doi/pdf/10.1021/acs.est.6c00693?ref=article_openPDF | PDF captured |
| aip | verified | 10.1063/5.0237567 | doi_epdf | https://pubs.aip.org/doi/epdf/10.1063/5.0237567 | PDF captured |
| ams | verified | 10.1175/aies-d-23-0093.1 | ams_downloadpdf_view | https://journals.ametsoc.org/downloadpdf/view/journals/aies/3/4/AIES-D-23-0093.1.pdf | PDF captured |
| annual-reviews | verified | 10.1146/annurev-phyto-011325-012824 | doi_pdf_to_docserver | https://annualreviews.org/doi/pdf/10.1146/annurev-phyto-011325-012824 | OpenAthens path was selected automatically, Tsinghua SSO/2FA was completed manually in visible CloakBrowser, and the Annual Reviews docserver PDF was captured. |
| aps | unsupported | 10.1103/PhysRevLett.128.161102 | pdf_path | https://journals.aps.org/prl/pdf/10.1103/PhysRevLett.128.161102 | APS article/PDF routes were discovered, but this publisher is marked unsupported for the reusable institutional-login workflow. The article page exposes /login_inst_user for institution-provided username/password, while OpenAthens/WebVPN/CARSI-style routes tested with Tsinghua did not authorize the sample PDF. Do not click the PRL navigation item named Accepted; use only the article PDF route for open-access APS content. |
| copernicus | verified | 10.5194/acp-24-1-2024 | direct_pdf | https://acp.copernicus.org/articles/24/1/2024/acp-24-1-2024.pdf | PDF captured |
| elsevier | pending | 10.1016/j.watres.2024.121507 | pii_pdfft_to_signed_asset | https://www.sciencedirect.com/science/article/pii/S0043135424004093/pdfft | ScienceDirect route is learned: DOI -> PII article -> /pdfft -> signed pdf.sciencedirectassets.com main.pdf when entitlement exists. Tsinghua SSO returned through auth.elsevier.com/ShibAuth/deliverInstCredentials, and earlier retry reached a signed main.pdf asset URL. The current blocker is ScienceDirect Cloudflare challenge on the PDF route; it remained on "Are you a robot?" during the 2026-06-07 retry, so no PDF bytes were captured. |
| frontiers | verified | 10.3389/fmicb.2026.1831710 | pdf_path | https://www.frontiersin.org/articles/10.3389/fmicb.2026.1831710/pdf | PDF captured |
| ieee | verified | 10.1109/jstqe.2026.3687110 | ieee_stamp_pdf | https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&isnumber=&arnumber=11493918 | IEEE verified through institutional access: article page -> Institutional Sign In -> Tsinghua University(OpenAthens) -> Tsinghua SSO/2FA in visible CloakBrowser -> returned through tlink.lib.tsinghua.edu.cn -> PDF button /stamp/stamp.jsp?tp=&arnumber=11493918 -> PDF captured and text matched the DOI record. |
| iop | verified | 10.1088/1361-648x/ae72dd | article_pdf_deeplink | https://iopscience.iop.org/article/10.1088/1361-648x/ae72dd/pdf | Article access wall was routed to myIOPscience deeplink, OpenAthens entityID was set to Tsinghua, user completed Tsinghua SSO/2FA in visible CloakBrowser, and the IOP PDF was captured. |
| mdpi | verified | 10.3390/foods10081757 | pdf_path | https://www.mdpi.com/2304-8158/10/8/1757/pdf | PDF captured |
| oxfordacademic | verified | 10.1093/nar/gkaa892 | oup_article_pdf | https://academic.oup.com/nar/article-pdf/49/D1/D10/5937080/gkaa892.pdf | PDF captured |
| plos | verified | 10.1371/journal.pone.0000001 | plos_printable_file | https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0000001&type=printable | PDF captured |
| pnas | verified | 10.1073/pnas.2309123120 | doi_epdf | https://www.pnas.org/doi/epdf/10.1073/pnas.2309123120 | PDF captured |
| royalsocietypublishing | verified | 10.1098/rsos.150470 | doi_pdf | https://royalsocietypublishing.org/doi/pdf/10.1098/rsos.150470 | PDF captured |
| rsc | verified | 10.1039/d5cp03829d | rsc_articlepdf | https://pubs.rsc.org/en/content/articlepdf/2026/cp/d5cp03829d | PDF captured |
| science | verified | 10.1126/sciadv.adp3964 | doi_epdf | https://www.science.org/doi/epdf/10.1126/sciadv.adp3964 | PDF captured |
| springer | verified | 10.1038/s41586-020-2649-2 | direct_pdf | https://www.nature.com/articles/s41586-020-2649-2.pdf | PDF captured |
| wiley | verified | 10.1002/adfm.202525261 | doi_pdfdirect | https://onlinelibrary.wiley.com/doi/pdfdirect/10.1002/adfm.202525261 | PDF captured |
| world-scientific | verified | 10.1142/s0218194026500348 | worldscientific_reader_pdf_viewer | https://www.worldscientific.com/doi/reader/10.1142/S0218194026500348 | Institution chain is learned and persisted: Institutional Access, Tsinghua selection/SSO, Terms Yes, institutional banner recognition, PDF/EPUB button, and World Scientific PDF viewer capture. The downloaded PDF text matched the DOI record. |

## Pending Follow-up

- elsevier: ScienceDirect route is learned: DOI -> PII article -> /pdfft -> signed pdf.sciencedirectassets.com main.pdf when entitlement exists. Tsinghua SSO returned through auth.elsevier.com/ShibAuth/deliverInstCredentials, and earlier retry reached a signed main.pdf asset URL. The current blocker is ScienceDirect Cloudflare challenge on the PDF route; it remained on "Are you a robot?" during the 2026-06-07 retry, so no PDF bytes were captured.

## Unsupported

- aps: APS article/PDF routes were discovered, but this publisher is marked unsupported for the reusable institutional-login workflow. The article page exposes /login_inst_user for institution-provided username/password, while OpenAthens/WebVPN/CARSI-style routes tested with Tsinghua did not authorize the sample PDF. Do not click the PRL navigation item named Accepted; use only the article PDF route for open-access APS content.
