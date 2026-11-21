# IRA CARE privacy notice

**DRAFT FOR LEGAL REVIEW.** Written by the build to describe what the software actually does;
not reviewed by a lawyer or a data protection officer; not to be published until it has been.

## Who we are

IRA CARE is operated by the care organisation or family that runs it (the "operator"); the
operator is the data controller for the people whose data it holds. The software's maker is
not a party to the operator's processing unless a separate agreement says so. [Operator name,
address, ICO registration number and data protection contact to be inserted by the operator.]

## Whose data, and what

- **The person looked after:** name and preferred name; home address and, optionally, home
  coordinates; an emergency information summary and access notes written by their carer;
  heart rate, steps and movement from their watch; whether the watch is worn and its battery;
  the questions IRA asked and the answers (as summaries, and as text under consent); what
  they say to IRA; their location at the moment of an SOS; alerts and how they ended; the
  rolling daily summary IRA keeps so it can remember earlier today and this week.
- **Family members, carers and staff:** name, email address, phone number, role and
  escalation tier; sign-in credentials (as a hash); what they viewed (the access log).
- **Nobody else.** There is no advertising, analytics or tracking.

## Why, and on what basis

To check on the person, to notice when something may be wrong, and to tell the people who
look after them. The operator's lawful basis is for the operator to determine with advice;
the software is built on the person's consent for who may read what they said and who may
see their readings (recorded in the app with who gave it), and on the legitimate interest of
the people in the care circle in reaching the person when they do not answer.

## Where it goes

Data is stored on the operator's own server (the deployment guide places it on one machine
in a UK region). It is shown to the person's family and carers inside the service, under the
consents recorded. Messages (push, text, call) go to the people in the care circle through
the provider the operator configured; by default no provider is configured and nothing
leaves the machine. IRA CARE never contacts emergency services itself.

If the operator turns on a language model for conversation, the person's words are sent to
that model to compose replies; by default the model runs on the operator's own server. The
operator must say here which model and where it runs.

## How long

Readings, check-ins, transcripts, medication events, reports and the daily summaries are
removed after the retention period recorded for the person (12 months by default). Alerts
are removed after that period once resolved. The access log is kept separately as audit
evidence and is not removed with the person's data. The watch's location is kept only with
the alert it belongs to.

## Your rights

To see, correct and erase; to object; to withdraw consent. The person (or their legal
representative) withdraws a consent through their carer, who records it in the app; the
operator's manager can erase the person's record. Complaints go to the operator's data
protection contact and to the Information Commissioner's Office.

## Security

Access notes are encrypted at rest under a key held by the operator. Every read of health
data, transcripts or access notes is written to an access log. Connections use HTTPS.
Device tokens and pairing codes are stored only as hashes.

## Changes

This notice changes when the software changes; the version in the repository is the current
one and the operator publishes it with a date.
