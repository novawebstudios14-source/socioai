# Agente de voz Twilio — protótipo em português

Hospedagem: Render Free. O modelo de IA precisa ser conectado por LLM_BASE_URL, LLM_MODEL e LLM_API_KEY. Sem esses valores, o agente informa que está em teste de telefonia. Ele não tem acesso às ferramentas nem ao histórico do ChatGPT.

## Estado verificado

- 12 testes locais passaram.
- A API Trial aceitou To + Url e criou uma chamada real. Os parâmetros Twiml, From e controles extras haviam sido recusados.
- O primeiro webhook real foi rejeitado pela validação padrão. A versão atual permite um token individual com validade de três minutos, vinculado a uma chamada criada pelo endpoint de controle. Esse fluxo precisa ser verificado na chamada após a implantação.

## Hospedagem

Build: pip install -r phone-agent/requirements.txt

Start: gunicorn --chdir phone-agent --workers 1 --threads 1 --bind 0.0.0.0:$PORT 'app:create_app()'

Plano free, uma instância. RENDER_EXTERNAL_URL é detectado automaticamente. Defina GUNICORN_CMD_ARGS=--access-logfile /dev/null para não registrar URLs que contêm tokens individuais.

Variáveis: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, ALLOWED_PHONES e CONTROL_API_KEY (segredo aleatório de pelo menos 32 bytes). LLM_BASE_URL, LLM_MODEL e LLM_API_KEY conectam o provedor de IA.

## Iniciar chamadas

POST /calls com Authorization: Bearer CONTROL_API_KEY, Content-Type: application/json e Idempotency-Key único de 16 a 80 letras, números, sublinhado ou hífen. Corpo: {"to":"número E.164 autorizado"}.

O servidor cria uma sessão, chama a Twilio apenas com To e Url e vincula o CallSid retornado à sessão. O endereço individual não é devolvido ao cliente. Repetir a chave de idempotência não inicia outra ligação. Existe limite de uma sessão a cada três minutos por destinatário.

O webhook /voice sem token exige assinatura Twilio válida. Os endereços individuais também aceitam o token de sessão de 256 bits, somente para chamada previamente criada pelo endpoint autenticado e dentro da validade de três minutos. Se o callback informar CallSid, To ou AccountSid, esses valores devem corresponder à sessão. Tokens desconhecidos, expirados e sessões sem CallSid são rejeitados.

## Controles e limites

- Destinos autorizados em ALLOWED_PHONES; nenhum endpoint de discagem anônimo.
- Até sete respostas e encerramento por voz.
- O aplicativo não solicita gravação de áudio.
- Texto da conversa fica no SQLite para contexto. O disco do Render Free é efêmero; reinícios podem apagar sessões e interromper chamadas.
- O Render Free pode suspender por ociosidade. Abra /health antes do teste e aguarde status ok. Não se promete disponibilidade contínua.
- O Trial Twilio e os provedores de IA têm cotas próprias. Não existe promessa de chamadas gratuitas ilimitadas.

## Testes

Na pasta phone-agent: python -m unittest discover -s tests -v. Os testes não fazem ligações nem acessam IA externa.

## Referências

https://www.twilio.com/docs/usage/trials/try-out-voice
https://render.com/docs/free
https://console.groq.com/docs/rate-limits
