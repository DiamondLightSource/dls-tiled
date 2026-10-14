{{/*
Expand the name of the chart.
*/}}
{{- define "tiled.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "tiled.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "tiled.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "tiled.labels" -}}
helm.sh/chart: {{ include "tiled.chart" . }}
{{ include "tiled.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "tiled.selectorLabels" -}}
app.kubernetes.io/name: {{ include "tiled.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "tiled.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "tiled.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of a beamline's Deployment and Service. Called with (dict "root" $ "beamline" .).
Fails rather than truncates, so two beamlines can never collide on one name.
*/}}
{{- define "tiled.beamlineFullname" -}}
{{- $name := printf "%s-%s" (include "tiled.fullname" .root) .beamline.name }}
{{- if gt (len $name) 63 }}
{{- fail (printf "%q is longer than 63 characters, shorten the release or beamline name" $name) }}
{{- end }}
{{- $name }}
{{- end }}

{{/*
libpq-style connection env for the bundled postgres, used by the init container
and the tiled config, e.g. postgresql://$PGUSER:$PGPASSWORD@$PGHOST/$PGDATABASE
*/}}
{{- define "tiled.postgresqlEnv" -}}
- name: PGHOST
  value: {{ include "tiled.fullname" . }}-postgresql
- name: PGUSER
  value: {{ .Values.postgresql.user | quote }}
- name: PGDATABASE
  value: {{ .Values.postgresql.database | quote }}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      {{- toYaml .Values.postgresql.passwordSecret | nindent 6 }}
{{- end }}
