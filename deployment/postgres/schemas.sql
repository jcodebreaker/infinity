-- One database, one schema per service: the Omnigent engine owns `engine`. Postgres runs it once,
-- when it initializes an empty data volume.
CREATE SCHEMA IF NOT EXISTS engine;
