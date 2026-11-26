# WEDA-FALL: permission request (sent)

**Sent on 26 November 2026 at about 13:34 UTC** from the founders' Gmail account to
`joaojtmarques@gmail.com`, the address the dataset's README gives for questions ("Any questions you
may have, feel free to contact…"), asking for it to be passed to the co-author, Dr Plinio Moreno. The
publishers' pages (MDPI, PubMed Central) could not be read from the build environment, so the
co-author's own address was not used. **Awaiting a reply.** Until one is recorded in
`docs/DATASETS.md`, WEDA-FALL is not used again (training or evaluation) and is not redistributed.

Subject: *WEDA-FALL: confirming the ODC-By licence for use by a commercial fall-detection project*

What the message says:

- who IRA CARE is (a small early-stage UK company; the product is in development, not deployed,
  not a medical device);
- what was done: one evaluation run on 26 November 2026 on commit `9bf1515`, nothing trained, no
  threshold changed, the clone deleted afterwards, only summary results kept with the ODC-By notice
  and a citation of the paper; no data included in or published with anything of ours;
- that the run found a defect in our own countdown logic (since fixed) and measured our false-alarm
  rate on the older participants' daily activities;
- that the README and LICENSE state ODC-By 1.0, that a separate browser check by one of the founders
  did not find a clear licence statement, and that use is paused until the authors confirm;
- six questions: whether ODC-By 1.0 covers the whole dataset including the older participants'
  recordings; commercial evaluation; commercial training and deployment of trained weights (not the
  data); attribution wording (currently J. Marques and P. Moreno, "Online Fall Detection Using Wrist
  Devices", *Sensors* 2023, 23(3), 1146, doi:10.3390/s23031146, with the ODC-By notice and links);
  redistribution (none intended); any other conditions or fee;
- an offer to share the evaluation results;
- how data copies are handled (outside source code, encrypted, team-only access, no attempt to
  identify participants, deleted on request).

When a reply arrives, record it in `docs/DATASETS.md` (licence, date, who confirmed, any conditions)
and keep the reply itself with the company's legal records, not in this repository.
