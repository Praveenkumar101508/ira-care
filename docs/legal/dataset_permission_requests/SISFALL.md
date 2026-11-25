# Confirmation of the SisFall terms for a commercial care product (draft)

**Draft, not sent.** For the founders to review, complete and send.

Why this is still worth sending: the first IRA watch fall model (v1) was trained on SisFall,
downloaded from Zenodo record 22212281. That record states CC BY 4.0, but its creator field is
empty ("None"), it has no description, and it was published on 31 August 2026, so it does not
show that the authors chose that licence. Until the authors confirm, the commercial status of
that training data is unclear (`docs/DATASETS.md`, `docs/ai/MODEL_WEIGHTS.md`).

No IRA product carries a SisFall-trained model now. The owner took v1 out of release builds
(D-077) and then out of every build, debug and demonstration included (D-082). Every build
carries fall model v3, trained only on BITS-2 and UCI HAR (both CC BY 4.0; D-080). SisFall stays
out of every build until its original rights are established. The authors' answer decides whether
SisFall, with its older participants, may be used to train a future model; it does not affect
anything that ships today. The message below therefore asks about future use and says plainly
that no SisFall-trained model is in the product.

## How to reach them

- **Addressee:** the authors of SisFall, Angela Sucerquia, José David López and Jesús Francisco
  Vargas-Bonilla, SISTEMIC research group, Faculty of Engineering, Universidad de Antioquia,
  Medellín, Colombia.
- **Route:** the corresponding author named in the paper (*Sensors* 2017, 17(1), 198,
  doi:10.3390/s17010198), at the address printed in the paper, or the contact on the SISTEMIC
  project page, <http://sistemic.udea.edu.co/en/investigacion/proyectos/english-falls/>.
  Neither could be opened from the build environment on 25 November 2026; confirm the
  addressee and address there before sending.

## Message

Subject: SisFall: confirming the terms for use in a commercial fall-detection product

Dear Dr Sucerquia, Prof. López and Prof. Vargas-Bonilla,

We are IRA CARE, a small company founded by Praveen Kamineti and Sravani Perni. We are building
a care product for older people who live at home: a wrist-worn watch that reminds them of
routines and medication, lets them ask for help, and alerts the people who look after them
when it detects a fall.

SisFall, with its older participants, has been the most useful dataset for this work, and we
have cited it in our technical documents. We obtained it from a copy on Zenodo
(<https://zenodo.org/records/22212281>) that is labelled Creative Commons Attribution 4.0
(CC BY 4.0). That record does not name who published it, so we would like to confirm with you,
as the authors, that this is the licence you intended, before relying on it in a commercial
product.

**What we have done and would do with it**

- Trained and evaluated, as research, a small fall-detection model (a few thousand numeric
  weights) on SisFall's accelerometer recordings, resampled to the watch's rate, with a
  subject-wise split, together with the UCI HAR dataset.
- Because we could not confirm your terms, that model is not in our product, in any build, and
  it has not been published anywhere. No SisFall recording, or any part of one, is included in
  the product or published.
- With your agreement, we would like to train a future model on SisFall alongside other data,
  to run on the watch beside a rule-based detector. Only the trained weights would ship.

**How we handle the data**

- We do not redistribute the data, in whole or in part.
- It is stored outside our source repository, on the company's own encrypted storage,
  accessible only to the people working on the model.
- We will delete our copy, and tell you that we have, whenever you ask.
- We do not attempt to identify any participant.

**Citation**

In every publication, model card, technical document and product notice that describes the
model, we cite:

> A. Sucerquia, J. D. López, J. F. Vargas-Bonilla. "SisFall: A Fall and Movement Dataset".
> *Sensors* 2017, 17(1), 198. doi:10.3390/s17010198

and will use any other wording you prefer.

**What we are asking**

1. Is SisFall released under CC BY 4.0, as the Zenodo copy states? If not, under what terms?
2. May a model trained on SisFall be used in a commercial product in future, on the terms above?
3. Are there any further conditions you would like us to follow?

Thank you for making SisFall available.

Yours sincerely,

Praveen Kamineti and Sravani Perni
Founders, IRA CARE
[contact email]
