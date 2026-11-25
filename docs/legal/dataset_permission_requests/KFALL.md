# Request to use the KFall dataset in a commercial care product (draft)

**Draft, not sent.** For the founders to review, complete and send.

## How to reach them

- **Addressee:** the KFall dataset team, Korea Advanced Institute of Science and Technology
  (KAIST), the authors of the dataset: Xiaoqun Yu, Jaehyuk Jang and Shuping Xiong.
- **Route:** the access request on the dataset's official page,
  <https://sites.google.com/view/kfalldataset>. That page could not be opened from the build
  environment on 25 November 2026; a search-engine index of it says requests are made there and
  reviewed with the requester's identity and affiliation, and access is granted by email.
- If the form has no place for a commercial-use question, write to the corresponding author
  named in the paper (Frontiers in Aging Neuroscience, 2021, doi:10.3389/fnagi.2021.692865),
  at the address printed in the paper. Confirm the name and address there before sending.

## Message

Subject: Request for permission to use KFall in a commercial fall-detection product

Dear KFall team,

We are IRA CARE, a small company founded by Praveen Kamineti and Sravani Perni. We are building
a care product for older people who live at home: a wrist-worn watch that reminds them of
routines and medication, lets them ask for help, and alerts the people who look after them
when it detects a fall. A care team sees these alerts on a dashboard, and the person's family
sees them on a phone.

We would like to ask for your permission to use the KFall dataset to train and evaluate the
watch's fall-detection model. The product is commercial: it will be sold or offered to care
providers and families. We therefore want to be sure that our use is within your terms before
we download anything, and we have not downloaded the dataset.

**What we would do with it**

- Train and evaluate a small fall-detection model for a wrist-worn device, using KFall's
  motion recordings, alongside other datasets whose licences allow commercial use.
- Use KFall in particular as a cross-dataset test: a model trained on other data, measured on
  KFall's falls and daily activities, to show whether it generalises to people it has not seen.
- Only the trained model (a few thousand numeric weights) would ship in the product. No KFall
  recording, or any part of one, would be included in the product or published.

**How we would handle the data**

- We will not redistribute the data, in whole or in part, to anyone, and we will not upload it
  to any public service or repository.
- The data will be stored only on the company's own encrypted storage, accessible to the
  founders and named engineers working on the model, and kept out of our source repository.
- We will delete the data, and tell you that we have, whenever you ask.
- We will not attempt to identify any participant.

**Citation**

In every publication, model card, technical document and product notice that describes the
model, we will cite the dataset in the form you ask for, which we understand to be:

> X. Yu, J. Jang, S. Xiong. "A Large-Scale Open Motion Dataset (KFall) and Benchmark Algorithms
> for Detecting Pre-impact Fall of the Elderly Using Wearable Inertial Sensors". *Frontiers in
> Aging Neuroscience*, 2021. doi:10.3389/fnagi.2021.692865

**What we are asking**

1. May we use KFall to train and evaluate a model that ships in a commercial product, on the
   terms above?
2. If yes, are there any further conditions (a written agreement, a fee, an acknowledgement
   wording, a limit on the term of use)?
3. If commercial use is not possible, may we use it for internal evaluation only, with no
   model trained on it shipping?

Thank you for making this dataset and for considering our request. We are glad to answer any
questions or to send more detail about the product.

Yours sincerely,

Praveen Kamineti and Sravani Perni
Founders, IRA CARE
[contact email]
