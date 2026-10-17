# Agente de voz por telefone — protótipo Twilio

Recebe o webhook Custom TwiML URL do painel Try out Voice e conversa em português usando Gather/Say. Um modelo compatível com a API OpenAI gera as respostas; sem modelo configurado, o serviço informa que está apenas em teste de telefonia.

## Estado verificado

- Nove testes locais passaram, incluindo o fluxo /voice usado pelo painel, assinaturas GET/POST, destinatário autorizado, retries e encerramento.
- As tentativas anteriores de criação de chamada com Twiml e parâmetros extras foram recusadas pela conta Trial. Isso não prova que uma chamada com Url seja bloqueada.
- A ligação real com o webhook e o modelo ainda precisa ser validada. Código publicado não significa agente ativo.

## Implantação de teste no Render

Use o plano free, runtime python, a branch feature/phone-agent-trial deste repositório e estes comandos:

Build: pip install -r phone-agent/requirements.txt

Start: gunicorn --chdir phone-agent --workers 1 --threads 1 --bind 0.0.0.0:$PORT 'app:create_app()'

Configure as variáveis TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN e ALLOWED_PHONES (números completos separados por vírgula). Para chamadas iniciadas pelo painel, TWILIO_FROM_NUMBER não é necessário. A URL externa é detectada automaticamente via RENDER_EXTERNAL_URL.

Para IA, configure LLM_BASE_URL (URL base terminada em /v1), LLM_MODEL e LLM_API_KEY. No Groq, use https://api.groq.com/openai/v1 e um modelo disponível na conta. Não coloque credenciais em arquivos versionados. A disponibilidade gratuita depende da conta e dos limites do provedor.

Depois da implantação, acesse /health e verifique a resposta. No painel Twilio, selecione Custom e use a URL pública do serviço seguida de /voice. Inicie a chamada para um destinatário verificado e autorizado. O número de origem do fluxo Trial pode ser compartilhado pela Twilio e não significa que a conta possui um número próprio.

O plano gratuito do Render pode suspender o serviço quando ocioso. Abra /health e aguarde o serviço responder antes do teste. O prazo do webhook da conta Trial é de cinco segundos; o primeiro acesso após suspensão pode falhar. Esse plano é adequado para validar um protótipo, não para prometer atendimento permanente.

## Limites e privacidade

- Até sete respostas por chamada e encerramento ao dizer desligar, parar ou tchau.
- Gravação de áudio desativada no comando de criação local; este aplicativo não solicita gravação pelo painel.
- Texto da conversa armazenado em SQLite para manter contexto. No Render free, o disco é efêmero: reinício ou nova implantação pode apagar sessões e interromper chamadas.
- O comando local de criação tem limite de 120 segundos. As chamadas pelo painel obedecem aos limites da Twilio; o aplicativo encerra no limite de interações.
- Não há endpoint HTTP para iniciar chamadas. /voice apenas responde a webhooks Twilio assinados.
- O agente não acessa o histórico nem as ferramentas do ChatGPT e não executa ações externas.

## Desenvolvimento

Instale requirements.txt em um ambiente Python 3.11+ e execute python -m unittest discover -s tests -v na pasta phone-agent. Os testes não fazem ligações nem acessam um modelo externo.

## Referências

- https://www.twilio.com/docs/usage/trials/try-out-voice
- https://www.twilio.com/docs/usage/security
- https://render.com/docs/free
- https://console.groq.com/docs/rate-limits
