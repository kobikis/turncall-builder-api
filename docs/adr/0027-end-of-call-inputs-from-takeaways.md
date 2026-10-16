# End-of-call inputs can come from a takeaway

ADR-0021 split a Connected app's two triggers on purpose. Mid-call, the model
supplies arguments at runtime. At end of call nothing chooses at runtime, so an
[[Action]]'s arguments "must be decided up front": a channel picked from a list,
plus the call summary in the one input the catalog names (`summary_arg`).

That is enough for "post the summary to #calls". It is not enough to log a call
in a CRM, which is the next app the catalog adds (HubSpot, after Cal.com; #96).
A CRM entry needs values that only exist once the call has happened: the
caller's name, why they called, what was agreed. TurnCall already extracts
exactly those as **takeaways** (TurnCall ADR-0013): named, schema-checked fields
at `analysis.takeaways.<name>` in `call.ended`.

## Decision

**An end-of-call input is either fixed or bound to a takeaway field.** In
`call_ended.args`, a value is what it is today (a chosen id, a literal), or a
reference to a takeaway field:

```json
"call_ended": {"action": "hubspot-create-engagement",
               "args": {"contact_email": {"takeaway": "caller", "field": "email"},
                        "owner_id": "12345"},
               "summary_arg": "body"}
```

The generated handler reads the field from the `call.ended` it is answering. If
the field is missing or empty, the run is recorded as `unfilled`, the outcome
ADR-0018 already reserves for "the call had nothing to put in a required
input". It is not retried.

**Only takeaways attached to this Agent can be bound.** The Console offers their
fields in the input's picker, beside the provider's own values (ADR-0022). A
takeaway detached later leaves the Automation showing a broken binding rather
than silently sending nothing.

**Built with HubSpot, not ahead of it.** The first app that needs it defines the
shape. Cal.com, which comes first, books during the call and does not need it.

## Consequences

- `config.integrations[].call_ended.args` values become `string | {takeaway,
  field}`. Existing configs are all strings and stay valid.
- Code generation renders a lookup into `analysis.takeaways` for bound inputs.
- This closes ROADMAP V5's "binding an end-of-call action's inputs to takeaway
  fields".
