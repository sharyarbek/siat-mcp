-- Схема базы siat.db. Пересоздаётся загрузчиком целиком (load.py) из сырых файлов.

CREATE TABLE node (                 -- дерево каталога: разделы, подразделы, группы и индикаторы (листья)
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER,
    code      TEXT,
    level     INTEGER,              -- 0 = корневой раздел
    is_leaf   INTEGER NOT NULL,
    position  INTEGER,
    name_uz   TEXT, name_uzc TEXT, name_ru TEXT, name_en TEXT
);
CREATE INDEX node_parent ON node(parent_id);
CREATE INDEX node_code   ON node(code);

CREATE TABLE classifier (           -- вид разреза: Регионы, Вид деятельности, Страны, Продукты…
    id      INTEGER PRIMARY KEY,
    name_uz TEXT, name_uzc TEXT, name_ru TEXT, name_en TEXT
);

CREATE TABLE indicator (
    id                INTEGER PRIMARY KEY REFERENCES node(id),
    code              TEXT NOT NULL,
    classifier_id     INTEGER REFERENCES classifier(id),
    periodicity_raw   TEXT,         -- как в карточке: Yillik / Choraklik / Oylik / Yangilanmaydigan
    view_options      TEXT,         -- JSON-список видов визуализации на сайте
    updated_xlsx      TEXT,         -- дата обновления данных на сайте (= «Обновлено» на странице)
    n_items           INTEGER,      -- строк в таблице (элементов разреза)
    n_obs             INTEGER,      -- всего значений
    period_types      TEXT,         -- например "Y" или "QY"
    first_period      TEXT,
    last_period       TEXT,
    table_empty       INTEGER NOT NULL,
    has_zeros         INTEGER NOT NULL,   -- есть нули: могут означать «нет данных» (x-siat-missing-values: zero)
    cumulative_likely INTEGER NOT NULL,   -- эвристика: квартальный/месячный ряд, похоже, нарастающим итогом
    fetched_at        TEXT
);
CREATE INDEX indicator_code ON indicator(code);

CREATE TABLE indicator_meta (       -- только белый список ключей, без персональных данных
    indicator_id INTEGER NOT NULL REFERENCES indicator(id),
    key          TEXT NOT NULL,
    lang         TEXT NOT NULL,     -- uz | uzc | ru | en
    value        TEXT NOT NULL,
    PRIMARY KEY (indicator_id, key, lang)
);

CREATE TABLE item (                 -- строки таблицы индикатора = элементы разреза
    indicator_id INTEGER NOT NULL REFERENCES indicator(id),
    code         TEXT NOT NULL,     -- для Регионов: код СОАТО (1700 = Республика)
    position     INTEGER NOT NULL,
    is_root      INTEGER NOT NULL,  -- итоговая строка (например, «Республика Узбекистан»)
    name_uz TEXT, name_uzc TEXT, name_ru TEXT, name_en TEXT,
    PRIMARY KEY (indicator_id, code)
);

CREATE TABLE observation (
    indicator_id INTEGER NOT NULL,
    item_code    TEXT NOT NULL,
    period       TEXT NOT NULL,     -- исходная строка: 2025 / 2025-Q3 / 2025-M09 / …
    period_type  TEXT NOT NULL,     -- Y | Q | M | X (неизвестный формат)
    period_start TEXT,
    period_end   TEXT,
    value        REAL,
    PRIMARY KEY (indicator_id, item_code, period)
) WITHOUT ROWID;
CREATE INDEX obs_period ON observation(indicator_id, period_start);

-- Полнотекстовый поиск по каталогу. Токенизатор trigram ищет по кусочкам из 3 символов,
-- поэтому находит слова в любых падежах/с окончаниями. Тексты в нём уже нормализованы (textnorm.py).
CREATE VIRTUAL TABLE catalog_fts USING fts5(
    indicator_id UNINDEXED,
    name_ru, name_uz, name_uzc, name_en, path, extra,
    tokenize = 'trigram'
);

CREATE TABLE load_info (key TEXT PRIMARY KEY, value TEXT);
