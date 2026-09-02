-- Migración para la app paralela de cursos de nivelación.
-- Estructura: 3 módulos x 7 semanas = 21 semanas acumuladas.
-- Ejecutar una sola vez en Supabase > SQL Editor de la NUEVA aplicación/base.

begin;

alter table public.analysis_runs alter column total_weeks set default 21;
alter table public.student_snapshots alter column total_weeks set default 21;
alter table public.risk_configuration alter column course_weeks set default 21;

-- Elimina únicamente restricciones CHECK asociadas a week_number para recrearlas con 1..21.
do $$
declare r record;
begin
  for r in
    select conrelid::regclass as table_name, conname
    from pg_constraint
    where contype = 'c'
      and conrelid in (
        'public.analysis_runs'::regclass,
        'public.student_snapshots'::regclass,
        'public.course_activity_plan'::regclass
      )
      and pg_get_constraintdef(oid) ilike '%week_number%'
  loop
    execute format('alter table %s drop constraint %I', r.table_name, r.conname);
  end loop;
end $$;

alter table public.analysis_runs
  add constraint analysis_runs_week_number_check check (week_number between 1 and 21);
alter table public.student_snapshots
  add constraint student_snapshots_week_number_check check (week_number between 1 and 21);
alter table public.course_activity_plan
  add constraint course_activity_plan_week_number_check check (week_number between 1 and 21);

update public.risk_configuration
set course_weeks = 21,
    updated_at = now()
where config_name = 'default';

commit;
