-- Simplificação pedida pelo Carlos: marcações de 30 minutos, sem preços e sem email do cliente.
alter table services         drop column price_cents;
alter table calendar_entries drop column price_cents;
alter table calendar_entries drop column customer_email;

-- Horas oferecidas de 30 em 30 minutos por omissão.
alter table settings alter column slot_step_minutes set default 30;
update settings set slot_step_minutes = 30;
