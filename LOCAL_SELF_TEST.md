# Sócio IA — teste local no Windows

## 1. Requirements

- Windows 10/11
- Docker Desktop aberto, usando Linux containers
- Git e uma chave da Groq
- um número WhatsApp dedicado ao Sócio IA e seu celular pessoal

Nenhum VPS, domínio, HTTPS, Caddy, WSL ou pagamento é necessário.

## 2. Clone repository

~~~powershell
git clone https://github.com/novawebstudios14-source/socioai.git
Set-Location socioai
~~~

## 3. Create .env

~~~powershell
Copy-Item .env.local.example .env
notepad .env
~~~

Preencha somente estas cinco linhas. Use valores aleatórios longos e uma senha
PostgreSQL alfanumérica:

~~~dotenv
POSTGRES_PASSWORD=
EVOLUTION_API_KEY=
EVOLUTION_WEBHOOK_SECRET=
ADMIN_API_KEY=
GROQ_API_KEY=
~~~

Carregue os valores no PowerShell:

~~~powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^(.*?)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
  }
}
~~~

O Compose deriva bancos, URLs Docker internas, modelos Groq, onboarding e
transcrição. IA determinística é rejeitada neste modo.

## 4. Start Docker

~~~powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.local.yml ps
~~~

A primeira inicialização pode levar alguns minutos. API:
<http://localhost:8000/docs>. Evolution: <http://localhost:8080>.

## 5. Create Evolution instance

~~~powershell
$evoHeaders = @{ apikey = $env:EVOLUTION_API_KEY }
$createBody = @{ instanceName='socio-ia'; integration='WHATSAPP-BAILEYS'; qrcode=$true } | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri 'http://localhost:8080/instance/create' -Headers $evoHeaders -ContentType 'application/json' -Body $createBody
$created | ConvertTo-Json -Depth 10
~~~

Se a instância já existir após reiniciar, não a recrie.

## 6. Pair WhatsApp QR

~~~powershell
$connection = Invoke-RestMethod -Uri 'http://localhost:8080/instance/connect/socio-ia' -Headers $evoHeaders
$base64 = $connection.base64
if (-not $base64) { $base64 = $connection.qrcode.base64 }
$base64 = $base64 -replace '^data:image/[^;]+;base64,', ''
$qrPath = Join-Path $PWD 'socio-ia-qr.png'
[IO.File]::WriteAllBytes($qrPath, [Convert]::FromBase64String($base64))
Start-Process $qrPath
~~~

No WhatsApp do número dedicado, abra **Aparelhos conectados → Conectar um
aparelho** e leia o QR.

## 7. Configure webhook

Este endereço é interno à rede Docker; não use localhost, domínio ou HTTPS:

~~~powershell
$webhookBody = @{ webhook = @{
  enabled=$true
  url='http://api:8000/webhooks/evolution'
  webhookByEvents=$false
  webhookBase64=$true
  headers=@{ 'x-api-key'=$env:EVOLUTION_WEBHOOK_SECRET }
  events=@('MESSAGES_UPSERT')
}} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri 'http://localhost:8080/webhook/set/socio-ia' -Headers $evoHeaders -ContentType 'application/json' -Body $webhookBody
~~~

## 8. Send first WhatsApp message

Do seu WhatsApp pessoal, envie **Oi** ao número dedicado. A resposta deve chegar
pelo mesmo chat.

## 9. Complete onboarding

Responda, uma mensagem por vez: seu nome; **Nova Web Studios**; seu segmento;
seu principal objetivo; e **ACEITO**. A empresa será criada pelo telefone
remetente e ficará em **trial**.

## 10. Activate company

Troque o telefone pelo seu número pessoal com DDI, somente dígitos:

~~~powershell
$adminHeaders = @{ 'x-admin-key'=$env:ADMIN_API_KEY }
$phone = '55DDDNUMERO'
$company = Invoke-RestMethod -Uri "http://localhost:8000/internal/companies?phone=$phone" -Headers $adminHeaders
$company
Invoke-RestMethod -Method Patch -Uri "http://localhost:8000/internal/companies/$($company.company_id)/access" -Headers $adminHeaders -ContentType 'application/json' -Body '{"status":"active"}'
~~~

É a operação administrativa protegida existente; não há bypass público.

## 11. Run tests

1. **Memory:** “Minha empresa se chama Nova Web Studios.” Depois: “Qual é o nome da minha empresa?”
2. **Explicit memory:** “Guarda que meu contador se chama Carlos.” Depois: “Quem é meu contador?”
3. **Task:** “Preciso falar com o Carlos amanhã.” Depois: “O que tenho para amanhã?”
4. **Reminder:** “Me lembra daqui a 5 minutos de testar o lembrete.” Aguarde uma única mensagem.
5. **Audio:** envie “Me lembra amanhã de revisar uma proposta.” como áudio.
6. **PDF:** envie um PDF com texto, peça “Resume esse PDF.” e faça uma pergunta factual.
7. **Restart:** reinicie e pergunte novamente o nome da empresa.

~~~powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml restart api worker
~~~

### Health verification

Aguarde 30 segundos após iniciar o worker:

~~~powershell
$compose = @('-f','docker-compose.yml','-f','docker-compose.local.yml')
docker compose @compose ps
Invoke-RestMethod 'http://localhost:8000/health/operational'
Invoke-RestMethod 'http://localhost:8000/health/providers' -Headers $adminHeaders
Invoke-RestMethod 'http://localhost:8080/instance/connectionState/socio-ia' -Headers $evoHeaders
docker compose @compose exec redis redis-cli ping
docker compose @compose exec postgres pg_isready -U socioai -d socioai
docker compose @compose exec postgres psql -U socioai -d socioai -c "SELECT extname FROM pg_extension WHERE extname='vector';"
docker compose @compose exec api alembic current
~~~

Esperado: containers healthy/running, Redis **PONG**, PostgreSQL aceitando
conexões, extensão **vector**, Alembic em **head**, worker **alive**, Evolution
conectado e probes Groq com **ok: true**.

Os volumes permanecem após restart e down. Não execute down -v: isso apaga os
dados locais.
