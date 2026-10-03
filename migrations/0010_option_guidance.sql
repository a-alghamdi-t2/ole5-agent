-- Everything the agent needs to choose a field's value lives on the
-- Configuration page, where the team can change it; system.md keeps only how
-- to work (searching, when not to assert, writing a reply).
--
-- agent_option_kinds: for each of the seven fields, how to choose it -- the
-- guidance that is about the field, not about any one value ("judge priority
-- from impact, not tone").
--
-- agent_options.description: when to choose that value. Filled here for every
-- value that has none yet, from the old prompt, the product SLA (Jan 2025)
-- and the team scopes. A description someone has already written is kept.
--
-- From here on both are required: the database refuses an empty one.

CREATE TABLE agent_option_kinds (
    kind        TEXT PRIMARY KEY CHECK (kind IN ('service', 'queue', 'next_state', 'type',
                                                 'subtype', 'priority', 'sla')),
    guidance    TEXT NOT NULL CHECK (length(btrim(guidance)) > 0),
    updated_by  BIGINT REFERENCES reviewers(id),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER agent_option_kinds_touch BEFORE UPDATE ON agent_option_kinds
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

INSERT INTO agent_option_kinds (kind, guidance) VALUES
('service', $$The product the ticket is about. The Support queue receives mail for both products and the ticket does not say which: decide from the content. When it gives you nothing to go on, choose the likelier one and set confidence to low -- that is what tells the reviewer you were guessing.$$),

('queue', $$Where the ticket goes when it leaves the Support queue. Decide it first, before you think about how to reply. Choose on what the ticket is about -- not on whether you can answer it, and not on how much the customer told you; even a ticket too vague to act on is about something. Set it on every ticket, whatever the action.

The team scopes at the end describe what each team handles and where the lines between them fall; choose by them. Product Support is not the default: a ticket reporting something broken belongs with Product or Operations even when you cannot diagnose it. When a ticket is too vague to place, choose the queue its subject points at and set confidence to low. queue_reason says why this queue rather than the others; if you cannot give a reason, you have not chosen.$$),

('next_state', $$What happens to the ticket next. Waiting For Customer Response when you ask the customer for something; Waiting for Concerned Department when you route; open when you answer. The other states are set by people, not by you.$$),

('type', $$What the ticket is -- not how much you know about it. A system that is down is Incident / Urgent and a defect is Issue / Problem whether or not you can say why; not knowing the cause is normal, and is what missing and the routing are for. It does not make a fault an Inquiry: keep Inquiry for tickets that actually ask a question.$$),

('subtype', $$The specific kind of request, from the OLE5 branch. Choose the one that names the same thing the ticket reports. When none fits, choose Other -- a near miss misleads the team more than Other does.$$),

('priority', $$The severity of the problem as the product SLA defines it: judged from the impact on the customer's work -- how many users are affected, whether core features are lost, whether a workaround exists -- not from the tone. Capitals or the word urgent in a ticket are not impact; a polite ticket reporting that nobody can log in is still Critical. Problems outside production, such as a staging environment, are Low. When it is genuinely unclear, choose 1. Low.$$),

('sla', $$The service level the ticket is held to under the product SLA. Choose the level with the same severity as the priority you chose: a Critical problem is held to the Critical SLA, a Low one to the Low SLA. Response and resolution times below are for Ole5.$$);


-- ---------------------------------------------------------------- values
-- Only where there is no description yet.

CREATE TEMP TABLE _desc (kind TEXT, value TEXT, description TEXT);
INSERT INTO _desc VALUES
-- service
('service', 'OLE5 / Ole5 New', $$Administrative correspondence (Ole5, معاملاتي / الاتصالات الإدارية): correspondence, inbox, delivery statements, stickers, archive, users, departments, licences. Almost every ticket.$$),
('service', 'RiCH', $$The RiCH platform, including its SMS service (OTP, notifications, campaigns). Always route a RiCH ticket; it is not ours to answer.$$),

-- queue: one line each; the team scopes carry the full description
('queue', 'Ole5 New::Customer Success', $$The relationship rather than the system: meetings, training, onboarding, contracts and commercial questions about licences, compliance documents, complaints about service, approvals needed before work can be done.$$),
('queue', 'Ole5 New::Operations', $$Anything that needs access to the customer's tenant, data or infrastructure: live faults, outages, database scripts and extractions, whitelisting, mail/OTP/SMS delivery, certificates and SSO, DNS, storage and licence limits, account work at the backend, deploying fixes.$$),
('queue', 'Ole5 New::Product', $$The product itself: defects and whether something is by design, enhancement and change requests, missing features. A defect belongs here even when its cause is unknown.$$),
('queue', 'Ole5 New::Product Support', $$Help using a product that is working: how to do something, permissions, what a screen or setting does, configuration the customer can do themselves.$$),
('queue', 'Junk', $$Spam, advertising and mail unrelated to our products only; never a customer request.$$),

-- next_state
('next_state', 'closed successful', $$Only for junk sent to the Junk queue. Never for a real customer's ticket, even one you have answered completely: they may write back, and a person closes it once they are sure.$$),
('next_state', 'open', $$When you answer and nothing further is awaited from anyone.$$),
('next_state', 'pending auto close+', $$Set by people, not by you: the ticket closes by itself after a waiting period.$$),
('next_state', 'pending reminder', $$Set by people, not by you: a reminder to come back to the ticket later.$$),
('next_state', 'Waiting for Concerned Department', $$When you route: the ticket waits for the team of the queue you chose.$$),
('next_state', 'Waiting For Customer Response', $$When you ask the customer for something (ask_more): the ticket waits for their answer.$$),
('next_state', 'Waiting for Service Provider', $$Set by people, not by you: the fix depends on an outside provider and the ticket waits on them.$$),

-- type
('type', 'Complaints', $$The customer complains about the service, a delay, or how they were treated -- not a fault report.$$),
('type', 'Default', $$Only when no other type fits at all. Avoid it: it tells the team nothing.$$),
('type', 'External / Vendor Support', $$The problem lies with an outside vendor or third-party system the customer uses alongside ours.$$),
('type', 'Feedback / Suggestion', $$An opinion or an idea about the product, with no fault to fix and no firm request.$$),
('type', 'Incident / Urgent', $$Something is down or unusable for many users now: a system outage, nobody can log in, incoming and outgoing transactions halted.$$),
('type', 'Inquiry', $$A question: how to do something, what something means. Only for tickets that actually ask a question.$$),
('type', 'Internal Tickets', $$Raised by our own staff for internal work, not by a customer.$$),
('type', 'Issue / Problem', $$Something is not working as it should for this customer: an error, a malfunction, wrong data -- whether or not the cause is known.$$),
('type', 'Junk', $$Spam, advertising, or mail unrelated to our products. Goes with the Junk queue.$$),
('type', 'New requirement', $$Asks for something the product does not do yet: a new feature or a change to how it works.$$),
('type', 'Request', $$Asks us to do routine work: create or update data, add or remove users, reset something, extract a report.$$),
('type', 'Service Failure', $$A service we provide failed to be delivered -- notifications, emails or SMS not sent -- rather than a screen or feature malfunctioning.$$),

-- subtype
('subtype', 'OLE5::Bug::Software malfunction', $$A feature of the web system behaves wrongly or shows an error.$$),
('subtype', 'OLE5::Infrastructure::System Down', $$The system is down or unreachable for the customer.$$),
('subtype', 'OLE5::Inquiry::General questions', $$A how-to or general question about using the system.$$),
('subtype', 'OLE5::Inquiry::Meeting discussion between product to customer', $$The customer asks for, or follows up on, a meeting with the product team.$$),
('subtype', 'OLE5::Inquiry::Meeting training session', $$A request for training or a training session.$$),
('subtype', 'OLE5::Issue::Configuration error', $$Something is set up wrongly for this customer: permissions, licence counts, settings, integration configuration.$$),
('subtype', 'OLE5::Issue::Documents issues', $$Problems with documents or attachments: uploading, viewing, printing, missing files.$$),
('subtype', 'OLE5::Mobile App Issue::Application crash', $$The mobile app closes or crashes.$$),
('subtype', 'OLE5::Mobile App Issue::Software malfunction', $$A mobile app feature behaves wrongly, without crashing.$$),
('subtype', 'OLE5::Report::Reporting', $$A problem with an existing report: wrong figures, not loading, not exporting.$$),
('subtype', 'OLE5::Request for Integration::Integration with external systems', $$A request to connect the system with another system.$$),
('subtype', 'OLE5::Request for Migration::Data migration', $$A request to move data into or out of the system.$$),
('subtype', 'OLE5::Request for New Feature with Change Request::System enhancement', $$A request for a new feature or a change to how the system works.$$),
('subtype', 'OLE5::Request::Customer feedback', $$Feedback from the customer, positive or negative, with no fault to fix.$$),
('subtype', 'OLE5::Request::Data update', $$A request to change data: users, departments, licences, correspondence details.$$),
('subtype', 'OLE5::Request::Reporting', $$A request for a report or data extraction that does not exist yet.$$),
('subtype', 'OLE5::Reset Password::No Password received', $$The user did not receive a password, OTP or reset email.$$),
('subtype', 'Other', $$None of the others fits. Better than a near miss.$$),

-- priority: the SLA severity levels
('priority', '1. Low', $$Low severity: no impact on the business -- how-to questions, documentation, general questions, enhancement requests, problems outside production. About 15% of the client base affected.$$),
('priority', '2. Medium', $$Medium severity: a feature fails but a workaround exists, or a few users with low impact are blocked, or an error that does not stop the work, or minimal slowness. About 50% of the client base affected.$$),
('priority', '3. High', $$High severity: core features lost with no acceptable workaround, multiple users affected, severely degraded performance, a component crashes or hangs, or key users blocked. About 80% of the client base affected.$$),
('priority', '5 Critical', $$Critical severity: production is down or unusable -- the system is down or not responding, no user can work, the system crashes. 100% of the client base affected.$$),
('priority', '7. Inquiry', $$Not an SLA severity level. A question is 1. Low; do not choose this.$$),
('priority', 'Default', $$Not an SLA severity level. Do not choose this.$$),
('priority', 'New Feature', $$Not an SLA severity level. An enhancement request is 1. Low; do not choose this.$$),

-- sla: the Ole5 table of the product SLA
('sla', 'SLA - Critical (Ole5 + Ole5 New + Availo)', $$For Critical severity. Response within 30 minutes, resolution within 4 hours, final resolution within 24 hours; root cause analysis within 24 hours (initial) and 72 hours (final).$$),
('sla', 'SLA - High (Ole5 + Ole5 New + Availo)', $$For High severity. Response within 1 hour, resolution within 24 hours, final resolution within 72 hours; root cause analysis within 48 hours (initial) and 96 hours (final).$$),
('sla', 'SLA - Mid (Ole5 + Ole5 New + Availo)', $$For Medium severity. Response within 4 hours, resolution within 72 hours, final resolution within 120 hours; may be scheduled on the product roadmap.$$),
('sla', 'SLA - Low (Ole5 + Ole5 New + Availo)', $$For Low severity: questions, requests, enhancements. Response within 8 hours, resolution within 120 hours, final resolution within 480 hours; may be scheduled on the product roadmap.$$);

UPDATE agent_options o
SET description = d.description
FROM _desc d
WHERE o.kind = d.kind AND o.value = d.value
  AND (o.description IS NULL OR btrim(o.description) = '');

DROP TABLE _desc;

-- ---------------------------------------------------------------- required from now on
-- NOT VALID first: it applies to every insert and update at once, whatever
-- rows exist. Then validated if nothing is left empty -- a value added on the
-- page that this file does not know about would otherwise stop the deploy.
-- Such a value is refused on its next change until it is described, and the
-- Configuration page marks it.
ALTER TABLE agent_options
    ADD CONSTRAINT agent_options_description_required
    CHECK (length(btrim(coalesce(description, ''))) > 0) NOT VALID;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM agent_options
                   WHERE length(btrim(coalesce(description, ''))) = 0) THEN
        ALTER TABLE agent_options VALIDATE CONSTRAINT agent_options_description_required;
    END IF;
END $$;
