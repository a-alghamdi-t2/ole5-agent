-- Baseline: the whole schema as it stood on 20260921_154933, taken from the
-- production database with pg_dump, plus the agent's option lists.
--
-- Replaces every migration before it; those are in migrations_archive/.
-- From here on, every schema change is a new numbered file after this
-- one. Never edit this file, and never change the schema by hand.

COMMENT ON SCHEMA public IS '';

CREATE TYPE public.actor AS ENUM (
    'intake',
    'orchestrator',
    'reviewer',
    'writer'
);

CREATE TYPE public.article_party AS ENUM (
    'request',
    'other'
);

CREATE TYPE public.doc_status AS ENUM (
    'pending',
    'indexed',
    'failed'
);

CREATE TYPE public.draft_action AS ENUM (
    'answer',
    'route'
);

CREATE TYPE public.draft_status AS ENUM (
    'pending',
    'approved',
    'rejected',
    'superseded'
);

CREATE TYPE public.reply_kind AS ENUM (
    'answer',
    'ask_more'
);

CREATE TYPE public.resolution_kind AS ENUM (
    'information',
    'action',
    'none'
);

CREATE TYPE public.review_outcome AS ENUM (
    'approve',
    'edit',
    'reject'
);

CREATE TYPE public.write_status AS ENUM (
    'pending',
    'sent',
    'failed'
);

CREATE FUNCTION public.touch_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE TABLE public.agent_options (
    id bigint NOT NULL,
    kind text NOT NULL,
    value text NOT NULL,
    description text,
    active boolean DEFAULT true NOT NULL,
    locked boolean DEFAULT false NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    created_by bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT agent_options_kind_check CHECK ((kind = ANY (ARRAY['service'::text, 'queue'::text, 'next_state'::text, 'type'::text, 'subtype'::text, 'priority'::text, 'sla'::text]))),
    CONSTRAINT agent_options_value_check CHECK ((length(btrim(value)) > 0))
);

CREATE SEQUENCE public.agent_options_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.agent_options_id_seq OWNED BY public.agent_options.id;

CREATE TABLE public.articles (
    id bigint NOT NULL,
    ticket_id bigint NOT NULL,
    otrs_article_id text NOT NULL,
    article_no integer,
    party public.article_party NOT NULL,
    via text,
    sender_name text,
    sender_address text,
    recipients text,
    cc text,
    subject text,
    body_raw text NOT NULL,
    body_clean text,
    body_quoted text,
    body_signature text,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    otrs_created_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.articles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.articles_id_seq OWNED BY public.articles.id;

CREATE TABLE public.audit_log (
    id bigint NOT NULL,
    ticket_id bigint,
    draft_id bigint,
    actor public.actor NOT NULL,
    action text,
    reasoning text,
    evidence jsonb,
    model text,
    prompt_ver text,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.audit_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.audit_log_id_seq OWNED BY public.audit_log.id;

CREATE TABLE public.boilerplate_lines (
    scope text NOT NULL,
    key text NOT NULL,
    line_key text NOT NULL,
    line text NOT NULL,
    seen_in integer NOT NULL,
    out_of integer NOT NULL,
    built_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT boilerplate_lines_scope_check CHECK ((scope = ANY (ARRAY['domain'::text, 'sender'::text, 'internal'::text])))
);

CREATE TABLE public.closed_articles (
    id bigint NOT NULL,
    closed_ticket_id bigint NOT NULL,
    otrs_article_id text NOT NULL,
    article_no integer,
    sender_type text,
    channel_id text,
    visible boolean,
    sender_name text,
    sender_address text,
    recipients text,
    cc text,
    subject text,
    body_raw text NOT NULL,
    body_clean text,
    body_quoted text,
    body_signature text,
    attachments jsonb DEFAULT '[]'::jsonb NOT NULL,
    otrs_created_at timestamp with time zone
);

CREATE SEQUENCE public.closed_articles_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.closed_articles_id_seq OWNED BY public.closed_articles.id;

CREATE TABLE public.closed_tickets (
    id bigint NOT NULL,
    otrs_ticket_id text NOT NULL,
    ticket_number text NOT NULL,
    title text,
    queue text,
    state text,
    type text,
    priority text,
    service text,
    sla text,
    owner text,
    responsible text,
    subtype text,
    conclusions text,
    requester_email text,
    customer_id_otrs text,
    customer_org text,
    otrs_created_at timestamp with time zone,
    otrs_closed_at timestamp with time zone,
    solution_minutes integer,
    article_count integer NOT NULL,
    noise_count integer NOT NULL,
    dynamic_fields jsonb DEFAULT '{}'::jsonb NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    search_text text,
    search_ver text
);

CREATE SEQUENCE public.closed_tickets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.closed_tickets_id_seq OWNED BY public.closed_tickets.id;

CREATE TABLE public.documents (
    id bigint NOT NULL,
    filename text NOT NULL,
    content_hash text NOT NULL,
    size_bytes bigint,
    uploaded_by bigint NOT NULL,
    status public.doc_status DEFAULT 'pending'::public.doc_status NOT NULL,
    ragent_doc_id text,
    chunk_count integer,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    indexed_at timestamp with time zone
);

CREATE SEQUENCE public.documents_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.documents_id_seq OWNED BY public.documents.id;

CREATE TABLE public.drafts (
    id bigint NOT NULL,
    ticket_id bigint NOT NULL,
    status public.draft_status DEFAULT 'pending'::public.draft_status NOT NULL,
    action public.draft_action NOT NULL,
    reply_kind public.reply_kind,
    reply_body text,
    queue text NOT NULL,
    next_state text NOT NULL,
    type text,
    subtype text,
    conclusions text,
    service text CONSTRAINT drafts_product_not_null NOT NULL,
    issue_type text,
    summary text NOT NULL,
    collected jsonb DEFAULT '{}'::jsonb NOT NULL,
    missing jsonb DEFAULT '[]'::jsonb NOT NULL,
    note text,
    confidence text,
    reasoning text,
    evidence jsonb,
    model text,
    prompt_ver text,
    latency_ms integer,
    built_to_article_id bigint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    priority text,
    sla text,
    queue_reason text
);

CREATE SEQUENCE public.drafts_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.drafts_id_seq OWNED BY public.drafts.id;

CREATE TABLE public.eval_results (
    id bigint NOT NULL,
    run_id text NOT NULL,
    case_id text NOT NULL,
    expected_action text,
    got_action text,
    action_agrees boolean,
    expected_queue text,
    got_queue text,
    expected_type text,
    got_type text,
    expected_subtype text,
    got_subtype text,
    expected_priority text,
    got_priority text,
    reply_body text,
    reasoning text,
    elapsed_seconds real,
    error text,
    expected jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.eval_results_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.eval_results_id_seq OWNED BY public.eval_results.id;

CREATE TABLE public.eval_runs (
    id text NOT NULL,
    source_file text NOT NULL,
    queue text,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    total integer,
    agreed integer
);

CREATE TABLE public.kb_entries (
    id bigint NOT NULL,
    closed_ticket_id bigint NOT NULL,
    closed_article_id bigint,
    question text NOT NULL,
    answer_md text,
    has_answer boolean GENERATED ALWAYS AS ((answer_md IS NOT NULL)) STORED,
    language text,
    service text,
    queue text,
    subtype text,
    confidence text,
    model text,
    prompt_version text,
    raw_response jsonb,
    extracted_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.kb_entries_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.kb_entries_id_seq OWNED BY public.kb_entries.id;

CREATE TABLE public.otrs_outbox (
    id bigint NOT NULL,
    draft_id bigint NOT NULL,
    payload jsonb NOT NULL,
    status public.write_status DEFAULT 'pending'::public.write_status NOT NULL,
    attempts smallint DEFAULT 0 NOT NULL,
    last_error text,
    otrs_article_id text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    delivered_at timestamp with time zone
);

CREATE SEQUENCE public.otrs_outbox_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.otrs_outbox_id_seq OWNED BY public.otrs_outbox.id;

CREATE TABLE public.replays (
    id bigint NOT NULL,
    closed_ticket_id bigint NOT NULL,
    run_id text NOT NULL,
    expected_action text NOT NULL,
    got_action text NOT NULL,
    expected jsonb NOT NULL,
    got jsonb NOT NULL,
    reply_body text,
    reasoning text,
    corrections jsonb DEFAULT '[]'::jsonb NOT NULL,
    searches jsonb DEFAULT '[]'::jsonb NOT NULL,
    model text,
    prompt_ver text,
    seconds numeric(6,1),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.replays_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.replays_id_seq OWNED BY public.replays.id;

CREATE TABLE public.reviewers (
    id bigint NOT NULL,
    email text NOT NULL,
    display_name text,
    password_hash text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.reviewers_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.reviewers_id_seq OWNED BY public.reviewers.id;

CREATE TABLE public.reviews (
    id bigint NOT NULL,
    draft_id bigint NOT NULL,
    reviewer_id bigint NOT NULL,
    outcome public.review_outcome NOT NULL,
    final jsonb,
    changed_fields text[] DEFAULT '{}'::text[] NOT NULL,
    justification text,
    comment text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    changes jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.reviews_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.reviews_id_seq OWNED BY public.reviews.id;

CREATE TABLE public.sessions (
    token text NOT NULL,
    reviewer_id bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.ticket_extractions (
    id bigint NOT NULL,
    closed_ticket_id bigint NOT NULL,
    request_raw text,
    question text NOT NULL,
    missing jsonb DEFAULT '[]'::jsonb NOT NULL,
    answer text,
    model text,
    prompt_ver text,
    latency_ms integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    resolution public.resolution_kind
);

CREATE SEQUENCE public.ticket_extractions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ticket_extractions_id_seq OWNED BY public.ticket_extractions.id;

CREATE TABLE public.tickets (
    id bigint NOT NULL,
    otrs_ticket_id text NOT NULL,
    ticket_number text NOT NULL,
    title text,
    queue text,
    state text,
    type text,
    priority text,
    service text,
    sla text,
    lock text,
    owner text,
    responsible text,
    subtype text,
    conclusions text,
    requester_email text NOT NULL,
    customer_id_otrs text,
    customer_email text,
    customer_org text,
    otrs_created_at timestamp with time zone,
    first_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    last_synced_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    search_text text,
    search_ver text
);

CREATE SEQUENCE public.tickets_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.tickets_id_seq OWNED BY public.tickets.id;

ALTER TABLE ONLY public.agent_options ALTER COLUMN id SET DEFAULT nextval('public.agent_options_id_seq'::regclass);

ALTER TABLE ONLY public.articles ALTER COLUMN id SET DEFAULT nextval('public.articles_id_seq'::regclass);

ALTER TABLE ONLY public.audit_log ALTER COLUMN id SET DEFAULT nextval('public.audit_log_id_seq'::regclass);

ALTER TABLE ONLY public.closed_articles ALTER COLUMN id SET DEFAULT nextval('public.closed_articles_id_seq'::regclass);

ALTER TABLE ONLY public.closed_tickets ALTER COLUMN id SET DEFAULT nextval('public.closed_tickets_id_seq'::regclass);

ALTER TABLE ONLY public.documents ALTER COLUMN id SET DEFAULT nextval('public.documents_id_seq'::regclass);

ALTER TABLE ONLY public.drafts ALTER COLUMN id SET DEFAULT nextval('public.drafts_id_seq'::regclass);

ALTER TABLE ONLY public.eval_results ALTER COLUMN id SET DEFAULT nextval('public.eval_results_id_seq'::regclass);

ALTER TABLE ONLY public.kb_entries ALTER COLUMN id SET DEFAULT nextval('public.kb_entries_id_seq'::regclass);

ALTER TABLE ONLY public.otrs_outbox ALTER COLUMN id SET DEFAULT nextval('public.otrs_outbox_id_seq'::regclass);

ALTER TABLE ONLY public.replays ALTER COLUMN id SET DEFAULT nextval('public.replays_id_seq'::regclass);

ALTER TABLE ONLY public.reviewers ALTER COLUMN id SET DEFAULT nextval('public.reviewers_id_seq'::regclass);

ALTER TABLE ONLY public.reviews ALTER COLUMN id SET DEFAULT nextval('public.reviews_id_seq'::regclass);

ALTER TABLE ONLY public.ticket_extractions ALTER COLUMN id SET DEFAULT nextval('public.ticket_extractions_id_seq'::regclass);

ALTER TABLE ONLY public.tickets ALTER COLUMN id SET DEFAULT nextval('public.tickets_id_seq'::regclass);

ALTER TABLE ONLY public.agent_options
    ADD CONSTRAINT agent_options_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_otrs_article_id_key UNIQUE (otrs_article_id);

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.boilerplate_lines
    ADD CONSTRAINT boilerplate_lines_pkey PRIMARY KEY (scope, key, line_key);

ALTER TABLE ONLY public.closed_articles
    ADD CONSTRAINT closed_articles_otrs_article_id_key UNIQUE (otrs_article_id);

ALTER TABLE ONLY public.closed_articles
    ADD CONSTRAINT closed_articles_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.closed_tickets
    ADD CONSTRAINT closed_tickets_otrs_ticket_id_key UNIQUE (otrs_ticket_id);

ALTER TABLE ONLY public.closed_tickets
    ADD CONSTRAINT closed_tickets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.closed_tickets
    ADD CONSTRAINT closed_tickets_ticket_number_key UNIQUE (ticket_number);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_content_hash_key UNIQUE (content_hash);

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.drafts
    ADD CONSTRAINT drafts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_run_id_case_id_key UNIQUE (run_id, case_id);

ALTER TABLE ONLY public.eval_runs
    ADD CONSTRAINT eval_runs_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.kb_entries
    ADD CONSTRAINT kb_entries_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.otrs_outbox
    ADD CONSTRAINT otrs_outbox_draft_id_key UNIQUE (draft_id);

ALTER TABLE ONLY public.otrs_outbox
    ADD CONSTRAINT otrs_outbox_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.replays
    ADD CONSTRAINT replays_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.replays
    ADD CONSTRAINT replays_run_id_closed_ticket_id_key UNIQUE (run_id, closed_ticket_id);

ALTER TABLE ONLY public.reviewers
    ADD CONSTRAINT reviewers_email_key UNIQUE (email);

ALTER TABLE ONLY public.reviewers
    ADD CONSTRAINT reviewers_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_draft_id_key UNIQUE (draft_id);

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (token);

ALTER TABLE ONLY public.ticket_extractions
    ADD CONSTRAINT ticket_extractions_closed_ticket_id_key UNIQUE (closed_ticket_id);

ALTER TABLE ONLY public.ticket_extractions
    ADD CONSTRAINT ticket_extractions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_otrs_ticket_id_key UNIQUE (otrs_ticket_id);

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_ticket_number_key UNIQUE (ticket_number);

CREATE INDEX eval_results_misses_idx ON public.eval_results USING btree (run_id) WHERE (NOT action_agrees);

CREATE INDEX eval_results_run_idx ON public.eval_results USING btree (run_id);

CREATE INDEX idx_agent_options_kind ON public.agent_options USING btree (kind, "position");

CREATE INDEX idx_articles_ticket ON public.articles USING btree (ticket_id, article_no);

CREATE INDEX idx_audit_ticket ON public.audit_log USING btree (ticket_id, created_at);

CREATE INDEX idx_boilerplate_internal ON public.boilerplate_lines USING btree (scope) WHERE (scope = 'internal'::text);

CREATE INDEX idx_closed_articles_ticket ON public.closed_articles USING btree (closed_ticket_id, article_no);

CREATE INDEX idx_closed_queue ON public.closed_tickets USING btree (queue, otrs_closed_at);

CREATE INDEX idx_closed_subtype ON public.closed_tickets USING btree (subtype);

CREATE INDEX idx_documents_status ON public.documents USING btree (status, created_at);

CREATE INDEX idx_drafts_pending ON public.drafts USING btree (created_at) WHERE (status = 'pending'::public.draft_status);

CREATE INDEX idx_extractions_ticket ON public.ticket_extractions USING btree (closed_ticket_id);

CREATE INDEX idx_outbox_pending ON public.otrs_outbox USING btree (created_at) WHERE (status = 'pending'::public.write_status);

CREATE INDEX idx_replays_run ON public.replays USING btree (run_id);

CREATE INDEX idx_reviews_reviewer ON public.reviews USING btree (reviewer_id, created_at);

CREATE INDEX idx_sessions_expiry ON public.sessions USING btree (expires_at);

CREATE INDEX idx_sessions_reviewer ON public.sessions USING btree (reviewer_id);

CREATE INDEX kb_entries_service_lang_idx ON public.kb_entries USING btree (service, language) WHERE has_answer;

CREATE INDEX kb_entries_ticket_idx ON public.kb_entries USING btree (closed_ticket_id);

CREATE UNIQUE INDEX kb_entries_ticket_uidx ON public.kb_entries USING btree (closed_ticket_id);

CREATE UNIQUE INDEX uq_agent_options_value ON public.agent_options USING btree (kind, lower(value));

CREATE UNIQUE INDEX uq_drafts_one_pending ON public.drafts USING btree (ticket_id) WHERE (status = 'pending'::public.draft_status);

CREATE TRIGGER agent_options_touch BEFORE UPDATE ON public.agent_options FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

CREATE TRIGGER tickets_touch BEFORE UPDATE ON public.tickets FOR EACH ROW EXECUTE FUNCTION public.touch_updated_at();

ALTER TABLE ONLY public.agent_options
    ADD CONSTRAINT agent_options_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.reviewers(id);

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tickets(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_draft_id_fkey FOREIGN KEY (draft_id) REFERENCES public.drafts(id);

ALTER TABLE ONLY public.audit_log
    ADD CONSTRAINT audit_log_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tickets(id);

ALTER TABLE ONLY public.closed_articles
    ADD CONSTRAINT closed_articles_closed_ticket_id_fkey FOREIGN KEY (closed_ticket_id) REFERENCES public.closed_tickets(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.reviewers(id);

ALTER TABLE ONLY public.drafts
    ADD CONSTRAINT drafts_built_to_article_id_fkey FOREIGN KEY (built_to_article_id) REFERENCES public.articles(id);

ALTER TABLE ONLY public.drafts
    ADD CONSTRAINT drafts_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.tickets(id);

ALTER TABLE ONLY public.eval_results
    ADD CONSTRAINT eval_results_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.eval_runs(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.kb_entries
    ADD CONSTRAINT kb_entries_closed_article_id_fkey FOREIGN KEY (closed_article_id) REFERENCES public.closed_articles(id) ON DELETE SET NULL;

ALTER TABLE ONLY public.kb_entries
    ADD CONSTRAINT kb_entries_closed_ticket_id_fkey FOREIGN KEY (closed_ticket_id) REFERENCES public.closed_tickets(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.otrs_outbox
    ADD CONSTRAINT otrs_outbox_draft_id_fkey FOREIGN KEY (draft_id) REFERENCES public.drafts(id);

ALTER TABLE ONLY public.replays
    ADD CONSTRAINT replays_closed_ticket_id_fkey FOREIGN KEY (closed_ticket_id) REFERENCES public.closed_tickets(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_draft_id_fkey FOREIGN KEY (draft_id) REFERENCES public.drafts(id);

ALTER TABLE ONLY public.reviews
    ADD CONSTRAINT reviews_reviewer_id_fkey FOREIGN KEY (reviewer_id) REFERENCES public.reviewers(id);

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_reviewer_id_fkey FOREIGN KEY (reviewer_id) REFERENCES public.reviewers(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ticket_extractions
    ADD CONSTRAINT ticket_extractions_closed_ticket_id_fkey FOREIGN KEY (closed_ticket_id) REFERENCES public.closed_tickets(id) ON DELETE CASCADE;

INSERT INTO public.agent_options (kind, value, description, active, locked, position) VALUES
    ('next_state', 'closed successful', NULL, true, false, 1),
    ('next_state', 'open', NULL, true, true, 2),
    ('next_state', 'pending auto close+', NULL, true, false, 3),
    ('next_state', 'pending reminder', NULL, true, false, 4),
    ('next_state', 'Waiting for Concerned Department', NULL, true, true, 5),
    ('next_state', 'Waiting For Customer Response', NULL, true, true, 6),
    ('next_state', 'Waiting for Service Provider', NULL, true, false, 7),
    ('priority', '1. Low', NULL, true, true, 1),
    ('priority', '2. Medium', NULL, true, false, 2),
    ('priority', '3. High', NULL, true, false, 3),
    ('priority', '5 Critical', NULL, true, false, 4),
    ('priority', '7. Inquiry', NULL, true, false, 5),
    ('priority', 'Default', NULL, true, false, 6),
    ('priority', 'New Feature', NULL, true, false, 7),
    ('queue', 'Ole5 New::Customer Success', NULL, true, true, 1),
    ('queue', 'Ole5 New::Operations', NULL, true, true, 2),
    ('queue', 'Ole5 New::Product', NULL, true, true, 3),
    ('queue', 'Ole5 New::Product Support', NULL, true, true, 4),
    ('service', 'OLE5 / Ole5 New', NULL, true, true, 1),
    ('service', 'RiCH', NULL, true, true, 2),
    ('sla', 'SLA - Critical (Ole5 + Ole5 New + Availo)', NULL, true, false, 1),
    ('sla', 'SLA - High (Ole5 + Ole5 New + Availo)', NULL, true, false, 2),
    ('sla', 'SLA - Mid (Ole5 + Ole5 New + Availo)', NULL, true, false, 3),
    ('sla', 'SLA - Low (Ole5 + Ole5 New + Availo)', NULL, true, true, 4),
    ('subtype', 'OLE5::Bug::Software malfunction', NULL, true, false, 1),
    ('subtype', 'OLE5::Infrastructure::System Down', NULL, true, false, 2),
    ('subtype', 'OLE5::Inquiry::General questions', NULL, true, false, 3),
    ('subtype', 'OLE5::Inquiry::Meeting discussion between product to customer', NULL, true, false, 4),
    ('subtype', 'OLE5::Inquiry::Meeting training session', NULL, true, false, 5),
    ('subtype', 'OLE5::Issue::Configuration error', NULL, true, false, 6),
    ('subtype', 'OLE5::Issue::Documents issues', NULL, true, false, 7),
    ('subtype', 'OLE5::Mobile App Issue::Application crash', NULL, true, false, 8),
    ('subtype', 'OLE5::Mobile App Issue::Software malfunction', NULL, true, false, 9),
    ('subtype', 'OLE5::Report::Reporting', NULL, true, false, 10),
    ('subtype', 'OLE5::Request for Integration::Integration with external systems', NULL, true, false, 11),
    ('subtype', 'OLE5::Request for Migration::Data migration', NULL, true, false, 12),
    ('subtype', 'OLE5::Request for New Feature with Change Request::System enhancement', NULL, true, false, 13),
    ('subtype', 'OLE5::Request::Customer feedback', NULL, true, false, 14),
    ('subtype', 'OLE5::Request::Data update', NULL, true, false, 15),
    ('subtype', 'OLE5::Request::Reporting', NULL, true, false, 16),
    ('subtype', 'OLE5::Reset Password::No Password received', NULL, true, false, 17),
    ('subtype', 'Other', NULL, true, true, 18),
    ('type', 'Complaints', NULL, true, false, 1),
    ('type', 'Default', NULL, true, true, 2),
    ('type', 'External / Vendor Support', NULL, true, false, 3),
    ('type', 'Feedback / Suggestion', NULL, true, false, 4),
    ('type', 'Incident / Urgent', NULL, true, true, 5),
    ('type', 'Inquiry', NULL, true, true, 6),
    ('type', 'Internal Tickets', NULL, true, false, 7),
    ('type', 'Issue / Problem', NULL, true, true, 8),
    ('type', 'Junk', NULL, true, false, 9),
    ('type', 'New requirement', NULL, true, false, 10),
    ('type', 'Request', NULL, true, false, 11),
    ('type', 'Service Failure', NULL, true, false, 12);
