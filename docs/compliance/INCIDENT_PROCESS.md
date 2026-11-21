# IRA CARE: incident, complaints and safety-issue process

**DRAFT FOR REVIEW BY A QUALIFIED PERSON.** A proposed process for the operator; nothing here is
in force until the operator adopts it and names the people in it.

## Definitions

- **Safety issue:** anything where the service may have contributed to harm or a near miss:
  a missed escalation, a late one, a fall not detected, a false alarm with consequences, a
  wrong address, a message that gave advice.
- **Security incident:** unauthorised access, a leak, a lost key or passphrase, a breach of
  the server.
- **Service incident:** the service down or degraded (the "monitoring paused" banner, failed
  health checks).
- **Complaint:** a person, family member, carer or organisation unhappy with the service.

## Who

- **Service owner** (operator): accountable; decides notifications.
- **Clinical Safety Officer** (to be appointed): owns the hazard log; classifies safety issues.
- **Data protection contact** (to be appointed): owns personal-data incidents and ICO
  notifications.
- **On-call operator:** the person `IRA_CARE_OPS_PHONE` reaches; first responder for service
  incidents.

## How issues arrive

- The dashboard banner and `/health/jobs` (service).
- A family member or carer resolving an alert with a note that says something went wrong.
- Email to the operator's support and security addresses (to be set up and published in the
  store listings and the privacy notice).
- Staff observation.

## Steps

1. **Record** within one working day: what happened, when, who was affected, how it was
   noticed. For safety issues, copy the alert id, the check-ins, the readings and the access
   log entries for the window (the history search and the reports provide these).
2. **Make safe** at once: for a service incident, restart (`docker compose up -d`), check
   `/health` and `/health/jobs`, and tell the circles affected that monitoring was paused
   between the two times; for a security incident, rotate the affected secret
   (`IRA_SECRET_KEY` signs everyone out; `IRA_CONNECTOR_ENCRYPTION_KEY` cannot be rotated
   without re-sealing the access notes, which is a development task); for a safety issue,
   check the person is safe first.
3. **Classify** (Clinical Safety Officer for safety; data protection contact for data):
   severity and likelihood on the hazard log scale; whether an existing hazard (H1 to H13)
   covers it or a new row is needed.
4. **Notify** as the law and contracts require: the people affected; the ICO within 72
   hours for a personal-data breach likely to result in a risk; the commissioning
   organisation; the regulator if the adviser's answers say so (`MHRA_ADVISER_QUESTIONS.md`,
   question 11).
5. **Fix**: a code change goes through the usual path (a test that reproduces it, the fix,
   CI, review, deployment) and the hazard log row is updated with the new control and test.
6. **Close** with a written note: what happened, why, what changed, who was told. Keep it
   with the hazard log.

## Complaints

Acknowledge within three working days; answer within twenty; say what was found and what
changed; say how to escalate (the operator's own complaints route and, for care
organisations in England, the relevant ombudsman or regulator). Keep complaints in the same
record as issues so patterns show.

## Review

Monthly: the list of issues and complaints, the hazard log, the soak and health numbers.
After any major or catastrophic classification: a review within a week with the service
owner and the Clinical Safety Officer.
