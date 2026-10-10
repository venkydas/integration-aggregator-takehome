{{- define "aggregator.labels" -}}
app.kubernetes.io/name: aggregator
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "aggregator.selectorLabels" -}}
app.kubernetes.io/name: aggregator
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
