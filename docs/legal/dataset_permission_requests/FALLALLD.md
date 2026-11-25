# Request to use the FallAllD dataset in a commercial care product (draft)

**Draft, not sent.** For the founders to review, complete and send.

## How to reach them

- **Addressee:** the authors of FallAllD, Majd Saleh and Régine Le Bouquin Jeannès (with Manal
  Abbas, a co-author of the paper).
- **Route:** the dataset's page on IEEE DataPort,
  <https://ieee-dataport.org/open-access/fallalld-comprehensive-dataset-human-falls-and-activities-daily-living>,
  which shows the submitting author and the licence field. That page could not be opened from
  the build environment on 25 November 2026, so neither the licence nor a contact route was
  read there.
- Otherwise, the corresponding author named in the paper (*IEEE Sensors Journal*,
  doi:10.1109/JSEN.2020.3018335), at the address printed in the paper. Confirm both there
  before sending.

## Message

Subject: Request for permission to use FallAllD in a commercial fall-detection product

Dear Dr Saleh and Prof. Le Bouquin Jeannès,

We are IRA CARE, a small company founded by Praveen Kamineti and Sravani Perni. We are building
a care product for older people who live at home: a wrist-worn watch that reminds them of
routines and medication, lets them ask for help, and alerts the people who look after them
when it detects a fall. A care team sees these alerts on a dashboard, and the person's family
sees them on a phone.

FallAllD is one of very few open datasets with a wrist-worn sensor recorded alongside falls,
which is exactly the placement of our device. We would like to ask whether we may use it to
train and evaluate the watch's fall-detection model. The product is commercial, and we could
not find a licence statement for the dataset itself, so we are asking you directly before we
use it. We have not downloaded the dataset from IEEE DataPort. (We noticed a derived copy on
Kaggle labelled CC0 by a third party; we have not relied on it, because only you can set the
terms.)

**What we would do with it**

- Train and evaluate a small fall-detection model for a wrist-worn device, using FallAllD's
  wrist recordings (accelerometer and gyroscope), alongside other datasets whose licences
  allow commercial use.
- Measure the model on FallAllD subjects it was not trained on, so that its reported accuracy
  is subject-independent.
- Only the trained model (a few thousand numeric weights) would ship in the product. No
  FallAllD recording, or any part of one, would be included in the product or published.

**How we would handle the data**

- We will not redistribute the data, in whole or in part, to anyone, and we will not upload it
  to any public service or repository.
- The data will be stored only on the company's own encrypted storage, accessible to the
  founders and named engineers working on the model, and kept out of our source repository.
- We will delete the data, and tell you that we have, whenever you ask.
- We will not attempt to identify any participant.

**Citation**

In every publication, model card, technical document and product notice that describes the
model, we will cite:

> M. Saleh, M. Abbas, R. L. B. Jeannès. "FallAllD: An Open Dataset of Human Falls and
> Activities of Daily Living for Classical and Deep Learning Applications". *IEEE Sensors
> Journal*. doi:10.1109/JSEN.2020.3018335

and the IEEE DataPort record, in whatever form you prefer.

**What we are asking**

1. Under what licence is FallAllD released?
2. May we use it to train and evaluate a model that ships in a commercial product, on the terms
   above?
3. If yes, are there any further conditions (a written agreement, a fee, an acknowledgement
   wording)?

Thank you for publishing FallAllD and for considering our request.

Yours sincerely,

Praveen Kamineti and Sravani Perni
Founders, IRA CARE
[contact email]
