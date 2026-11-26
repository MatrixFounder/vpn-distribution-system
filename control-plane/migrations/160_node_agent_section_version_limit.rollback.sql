-- Откат 160: колонка версии раздела agent возвращается к CHECK 001.29f (не меньше 1) под его
-- прежним именем.

SET LOCAL ROLE app_owner;
SET LOCAL search_path TO control_plane;

ALTER TABLE nodes
    ADD CONSTRAINT nodes_agent_section_version_check CHECK (agent_section_version >= 1);
ALTER TABLE nodes DROP CONSTRAINT nodes_agent_section_version_range;
