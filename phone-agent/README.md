# Nova Voice Commands

Ponte de comandos por telefone para uma conversa Work do ChatGPT. A chamada usa Twilio Gather/Say em português. O bot de conversa usa Groq; a execução de tarefas acontece no Work, depois da conexão do plugin e da inscrição no evento.

## Uso na ligação

1. Diga: **Comando para o chat: crie um resumo sobre café.**
2. O assistente lê a transcrição de volta.
3. Diga **confirmar comando** para registrar e enfileirar o evento, ou **cancelar comando**.
4. Acompanhe a execução na conversa Work inscrita.

O registro não é descrito como execução concluída. Se o chat ainda não estiver inscrito, a chamada informa isso. Comandos anteriores à inscrição ficam disponíveis em `list_voice_commands`, mas não geram eventos retroativos.

## Configuração

Python 3.12. Instale `pip install -r requirements.txt` e execute `gunicorn --workers 1 --threads 1 --bind 0.0.0.0:$PORT 'app:create_app()'`. No repositório com subdiretório, acrescente `--chdir phone-agent`.

Variáveis de telefonia: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `ALLOWED_PHONES` (lista E.164 separada por vírgula), `CONTROL_API_KEY`, `PUBLIC_URL` (ou `RENDER_EXTERNAL_URL`). Na Trial, o transporte envia apenas To e Url; não atribui número próprio nem elimina os limites da Twilio.

Variáveis de IA: `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_API_KEY`, `LLM_MODEL=openai/gpt-oss-20b`.

Variáveis da ponte:

| Nome | Valor |
| --- | --- |
| `BRIDGE_ENABLED` | `true` habilita comandos na ligação |
| `BRIDGE_DATABASE_URL` | Internal Database URL do Postgres no mesmo workspace/região Render |
| `BRIDGE_ENCRYPTION_KEY` | Chave Fernet gerada uma vez; preservar entre deploys |
| `BRIDGE_FORM_KEY` | Segredo aleatório forte para assinar o consentimento OAuth |
| `BRIDGE_PAIRING_HASH` | SHA-256 hexadecimal do código privado de conexão |
| `GUNICORN_CMD_ARGS` | `--access-logfile /dev/null` evita registrar URLs de callbacks com tokens |

Não publique segredos no repositório. A ponte não usa SQLite em produção. Sem Postgres configurado, as operações privadas retornam 503 e a chamada não afirma ter salvo/enviado o comando.

## Conexão ao ChatGPT

1. Em ChatGPT Plugins, clique `+` → `Add custom MCP server`.
2. Nome: `Nova Voice Commands`; endpoint: `https://SEU-SERVICO/mcp`.
3. Use OAuth, com registro dinâmico de cliente (DCR). A descoberta publica os endpoints e o escopo `voice.commands`.
4. Autorize usando o código privado de conexão. Instale o plugin e habilite-o na conversa Work.
5. Peça ao Work para acompanhar `voice.command.confirmed`, sem filtros, e executar os pedidos confirmados nessa conversa. Antes de executar, deve ler e reservar cada comando com um `run_id` único. Finalize registrando o resultado.
6. Verifique uma confirmação real por telefone e o resultado no chat antes de considerar a integração concluída.

O servidor implementa MCP Events 2026-07-28, verificação de callback, assinatura Standard Webhooks e uma fila de entrega persistente. A instalação e a inscrição pertencem à conta ChatGPT do usuário; publicar o endpoint não as cria automaticamente.

## Persistência e entrega

Uma linha de estado cifrada com Fernet guarda comandos, inscrições, tokens OAuth e entregas. Transações Postgres com bloqueio da linha serializam alterações para um único titular. O mesmo comando e evento mantêm IDs estáveis nas tentativas. O worker faz até oito tentativas com atraso exponencial e variação aleatória. Respostas 410/413 não são repetidas. Após falha definitiva, o comando continua consultável; execução interrompida requer revisão humana antes de repetir uma ação externa.

Callbacks exigem HTTPS/443, IPs públicos validados no momento de conexão e certificado TLS para o hostname original. Não há redirecionamentos. A chave de assinatura pode rotacionar com janela de cinco minutos. Inscrições expiram em até sete dias, com padrão de um dia. Revogação OAuth encerra as conexões e inscrições deste único titular.

O plano Free do Render pode dormir após 15 minutos; o próximo acesso pode demorar cerca de um minuto. O Postgres Free criado em 17/10/2026 expira em **16/11/2026**. Não há garantia de operação permanente gratuita; migre os dados e a chave antes da expiração se quiser manter o serviço.

## Verificação

Execute a partir desta pasta: `python -m unittest discover -s tests -v`.

Os testes cobrem callbacks Twilio assinados e tokens por chamada, deduplicação, confirmação/cancelamento por voz, assinatura e validação do destino, verificação de desafio, persistência após recriação da aplicação, expiração, rotação, retries, reserva e conclusão de comandos, OAuth PKCE, CSRF, uso único dos códigos, refresh e recusa sem armazenamento. Entrega ao ChatGPT real depende de instalar o plugin e concluir a inscrição; testes locais usam receptor simulado e SQLite somente no ambiente de teste.

Documentação: [MCP Events](https://developers.openai.com/plugins/build/mcp-events), [OAuth](https://developers.openai.com/plugins/build/auth), [Conectar plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt), [limites Free Render](https://render.com/docs/free).
