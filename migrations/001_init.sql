-- Asosiy sxema. Pul va miqdor faqat NUMERIC: float yaxlitlash xatosi bermasin.

CREATE TABLE contractors (
    id                bigserial PRIMARY KEY,
    inn               text,
    name              text NOT NULL UNIQUE,      -- 1C'dagi aniq nom
    contract          text NOT NULL DEFAULT '',  -- 1C'dagi aniq shartnoma nomi
    contract_verified boolean NOT NULL DEFAULT true,
    active            boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX contractors_inn_uq ON contractors (inn) WHERE inn IS NOT NULL AND inn <> '';

-- Qog'ozdagi yozuv -> 1C kontragenti. Foydalanuvchi tasdiqlagan har bir moslik shu yerda o'rganiladi.
CREATE TABLE contractor_aliases (
    alias_norm    text PRIMARY KEY,
    contractor_id bigint NOT NULL REFERENCES contractors (id) ON DELETE CASCADE
);

CREATE TABLE materials (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL UNIQUE,  -- 1C'dagi aniq nom
    unit       text NOT NULL DEFAULT '',
    source     text NOT NULL DEFAULT 'learned' CHECK (source IN ('import', 'manual', 'learned')),
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE material_aliases (
    alias_norm  text PRIMARY KEY,
    material_id bigint NOT NULL REFERENCES materials (id) ON DELETE CASCADE
);

CREATE TABLE export_batches (
    id           bigserial PRIMARY KEY,
    filename     text NOT NULL,
    doc_count    integer NOT NULL,
    total_amount numeric(18, 2) NOT NULL,
    content      bytea NOT NULL,
    created_by   bigint NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE documents (
    id            bigserial PRIMARY KEY,
    -- processing -> draft -> confirmed -> exported; failed va cancelled alohida
    status        text NOT NULL DEFAULT 'processing'
                  CHECK (status IN ('processing', 'failed', 'draft', 'confirmed', 'exported', 'cancelled')),
    created_by    bigint NOT NULL,
    chat_id       bigint NOT NULL,
    image_path    text NOT NULL,
    image_hash    text NOT NULL,
    image_mime    text NOT NULL DEFAULT 'image/jpeg',
    error         text,
    ai_model      text,
    raw_ai        jsonb,
    ai_notes      jsonb NOT NULL DEFAULT '[]'::jsonb,
    supplier_raw  text,
    supplier_inn  text,
    receiver_raw  text,
    doc_number    text NOT NULL DEFAULT '',
    doc_date      date,
    contractor_id bigint REFERENCES contractors (id),
    match_method  text,
    match_score   integer,
    candidates    jsonb NOT NULL DEFAULT '[]'::jsonb,
    paper_total   numeric(18, 2),
    comment       text NOT NULL DEFAULT '',
    batch_id      bigint REFERENCES export_batches (id),
    confirmed_by  bigint,
    confirmed_at  timestamptz,
    exported_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
-- Bitta rasm ikki marta 1C'ga tushmasligi uchun.
CREATE UNIQUE INDEX documents_image_hash_uq ON documents (image_hash)
    WHERE status NOT IN ('cancelled', 'failed');
CREATE INDEX documents_status_idx ON documents (status, doc_date, id);
CREATE INDEX documents_dup_idx ON documents (contractor_id, doc_date) WHERE status <> 'cancelled';

CREATE TABLE document_lines (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    position    integer NOT NULL,
    raw_name    text NOT NULL DEFAULT '',
    name        text NOT NULL DEFAULT '',
    unit        text NOT NULL DEFAULT '',
    quantity    numeric(18, 3),
    price       numeric(18, 2),
    amount      numeric(18, 2),
    confidence  text NOT NULL DEFAULT 'high' CHECK (confidence IN ('high', 'medium', 'low')),
    match_note  text NOT NULL DEFAULT '',
    UNIQUE (document_id, position)
);

-- Har bir AI chaqiruvi: xarajat va nosozlikni kuzatish uchun.
CREATE TABLE ai_calls (
    id            bigserial PRIMARY KEY,
    document_id   bigint REFERENCES documents (id) ON DELETE SET NULL,
    model         text NOT NULL,
    input_tokens  integer,
    output_tokens integer,
    duration_ms   integer NOT NULL,
    ok            boolean NOT NULL,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
