# Sistema de Alerta Temprana — Cursos de Nivelación

Versión paralela de la aplicación semanal, adaptada exclusivamente para cursos organizados en **3 módulos de 7 semanas**. Mantiene el dashboard, motor de riesgo, mensajería, derivaciones, historial, Canvas LTI/OAuth2, token manual, base de bienestar y plan manual de actividades.

## Estructura temporal

La interfaz muestra:

- Módulo 1, semanas 1 a 7
- Módulo 2, semanas 1 a 7
- Módulo 3, semanas 1 a 7

Internamente se utilizan semanas acumuladas del 1 al 21 para conservar la compatibilidad con todos los cálculos históricos:

- Módulo 1 = semanas globales 1–7
- Módulo 2 = semanas globales 8–14
- Módulo 3 = semanas globales 15–21

## Instalación como app alterna

1. Cree un repositorio nuevo con todo el contenido de esta carpeta.
2. Configure los mismos secretos de Canvas y Supabase que usa la aplicación original, o utilice un proyecto Supabase independiente.
3. En Supabase ejecute `sql/schema.sql` si la base es nueva.
4. Si reutiliza una copia de la base anterior, ejecute `sql/migration_nivelacion_3_modulos_7_semanas.sql`.
5. Despliegue `app.py` en Streamlit Cloud.

## Uso recomendado

Antes del primer análisis de cada curso, abra **Plan semanal del curso** y asigne cada actividad a su módulo y semana. La meta acumulada tomará todas las actividades desde Módulo 1 · Semana 1 hasta el corte seleccionado.

## Validación

La versión fue compilada y se ejecutaron las pruebas automáticas incluidas en el proyecto: **18 pruebas aprobadas**.
