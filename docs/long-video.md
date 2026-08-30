# MVP de vídeos longos

Este fork adiciona um modo opcional à arquitetura existente. Use
`target_duration_minutes` com um inteiro entre **10 e 30**; `0` (padrão)
mantém o fluxo curto. O modo longo produz uma saída **1920×1080, 16:9**,
mesmo que a proporção padrão da instalação seja vertical.

## Preparação

Requisitos: Python 3.11+, uv e FFmpeg. Os testes de integração também usam
ffprobe. A validação local desta alteração foi feita com Python 3.11.

```sh
git clone --branch feat/long-video-mvp https://github.com/taiancarvalho/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
uv sync --frozen --python 3.11
```

A primeira execução cria `config.toml` a partir de `config.example.toml`.
Mantenha as credenciais apenas nesse arquivo, que é ignorado pelo Git.
Configure o provedor de LLM existente e a chave da fonte de mídia, por exemplo
`app.pexels_api_keys`. Nenhuma integração foi substituída. Escolher um LLM
local não torna os demais serviços locais: Edge TTS e busca de stock usam rede.

Na API, use `listen_host = "127.0.0.1"` para acesso apenas local; configure
`app.api_key` antes de expor o serviço em rede. O exemplo upstream usa
`0.0.0.0` por padrão. `app.ffmpeg_path` pode apontar para um FFmpeg instalado.

## CLI: gerar um vídeo

```sh
uv run --no-sync python cli.py \
  --video-subject "Como Roma se tornou uma potência" \
  --video-language pt-BR \
  --target-duration-minutes 10 \
  --voice-name pt-BR-AntonioNeural-Male \
  --video-source pexels \
  --bgm-type none
```

Troque `10` por `20` ou `30`. As opções de voz, volume, legendas, fonte,
transições, velocidade de clipes e materiais continuam sendo as existentes.
Para gerar apenas o roteiro, acrescente `--stop-at script`.

O novo controle fica na **CLI e API**. A WebUI não ganhou controles novos;
continua atendendo o fluxo original. Não use seu botão de gerar roteiro curto
esperando que ele gere automaticamente um roteiro de 30 minutos.

## Usar um roteiro pronto

Use texto de narração, sem instruções de cena ou títulos que não devem ser
falados. O texto será dividido automaticamente, sem descarte de conteúdo.
O valor de duração **não aumenta** um roteiro pronto curto.

É possível fornecer `--video-script`, mas um arquivo de lote JSON evita os
limites e problemas de escape da linha de comando:

```json
[
  {
    "video_subject": "Como Roma se tornou uma potência",
    "target_duration_minutes": 20,
    "video_language": "pt-BR",
    "video_script": "Substitua pelo roteiro completo. Separe os parágrafos com \n\n.",
    "voice_name": "pt-BR-AntonioNeural-Male",
    "video_source": "pexels",
    "bgm_type": ""
  }
]
```

Salve como `long-video.json` e execute:

```sh
uv run --no-sync python cli.py --batch-file long-video.json
```

Para mídia local, use `video_source: "local"` e
`video_materials: [{"provider": "local", "url": "./meu-video.mp4"}]`.
No lote, caminhos relativos são resolvidos a partir da pasta do JSON.
A CLI importa os arquivos para a pasta de mídia permitida pelo projeto.
Na API, envie previamente os arquivos pelo endpoint de upload existente.
Não foram relaxadas as proteções de acesso a arquivos.

## API

Inicie o servidor:

```sh
uv run --no-sync python main.py
```

Envie `POST http://127.0.0.1:8080/api/v1/videos` com JSON:

```json
{
  "video_subject": "Como Roma se tornou uma potência",
  "target_duration_minutes": 10,
  "video_language": "pt-BR",
  "voice_name": "pt-BR-AntonioNeural-Male",
  "video_source": "pexels",
  "bgm_type": "",
  "subtitle_enabled": true
}
```

Consulte `GET /api/v1/tasks/{task_id}`. O campo `videos` continua contendo
a saída final, compatível com os clientes atuais. Se houver autenticação,
envie `x-api-key` também ao baixar artefatos. A documentação interativa fica
em `http://127.0.0.1:8080/docs`.

## Processamento e arquivos

1. Sem roteiro fornecido, o mesmo dispatcher LLM gera um outline e depois
   cada capítulo, com orçamento estimado de 140 palavras/minuto × velocidade
   da voz. O contexto inclui outline, idioma, prompts e fim do capítulo anterior.
2. O texto é dividido em blocos de no máximo 2.500 caracteres e 280 palavras,
   preferindo limites de parágrafos/frases. Um capítulo editorial grande pode
   virar mais de um bloco de renderização. Idiomas e vozes têm ritmos diferentes.
3. Para cada bloco: termos de busca, TTS, legendas, mídia suficiente para a
   duração real do áudio e renderização com os helpers existentes.
4. Os blocos são processados sequencialmente. O FFmpeg concatena os MP4s com
   `-map 0:v:0 -map 0:a:0 -c copy`, sem abrir o vídeo inteiro no MoviePy e sem
   recodificar a saída final. Arquivos temporários ficam na mesma unidade;
   uma falha de concatenação não sobrescreve uma saída anterior.

```text
storage/tasks/<task-id>/
  script.json
  chapters.json
  chapters/001/
    script.json
    audio.mp3
    subtitle.srt       # quando disponível/habilitada
    combined-1.mp4
    final-1.mp4
  chapters/002/...
  final-1.mp4          # vídeo longo final
```

`chapters.json` registra texto, títulos, arquivos, duração medida e estado de
cada bloco. A resposta inclui `chapters`, `audio_duration`, `audio_files` e,
quando aplicável, `subtitle_paths`, `materials`, `videos` e `warnings`.
Os caminhos de diagnóstico do manifesto são locais ao servidor.

Falhas retornam `failed_stage` e `failed_chapter`; os capítulos concluídos
permanecem no disco. A etapa `concat` não tem capítulo específico.
O MVP **não retoma automaticamente** uma tarefa interrompida: uma nova execução
gera novamente os blocos e pode consumir créditos outra vez.

`--stop-at` aceita as mesmas etapas existentes. No modo longo, processa essa
etapa em todos os capítulos e retorna listas de artefatos. `script` não faz TTS;
`audio` retorna áudios separados; `subtitle` retorna SRTs com tempos locais
ao capítulo. Não há SRT global nem áudio único exportado separadamente.

## Duração, custos e limites

- A duração é uma **meta**, não uma garantia exata. A duração final acompanha
  a narração; desvios superiores a 15% geram `target_duration_mismatch`.
  Não cortamos fala nem repetimos texto para preencher a meta. O modelo recusa
  capítulos gerados claramente curtos, com tentativas limitadas.
- `video_count` deve ser 1. Áudio único enviado (`custom_audio_file`) e LoomLoom
  com orçamento confirmado são recusados no modo longo antes da geração.
  Continuam disponíveis no fluxo curto. Demais provedores reutilizam suas
  regras atuais; limites menores de um provedor ainda podem exigir blocos menores.
- Há mais chamadas de LLM, TTS, busca e, se selecionada, geração de mídia/música.
  Serviços pagos continuam cobrando conforme o provedor. O render e a concatenação
  são locais; os testes desta alteração não usam serviços pagos.
- Termos automáticos são gerados por bloco. `video_terms` fornecido explicitamente
  é reutilizado em cada bloco; mídia local usa a mesma coleção em todos os blocos.
  Não há garantia de exclusividade visual entre capítulos; o pipeline pode repetir
  material quando a coleção for insuficiente.
- Música é resolvida por capítulo: pode reiniciar/mudar entre capítulos.
  Para o primeiro teste, use música desativada. Uma trilha contínua fica para depois.
- **Sem publicação automática no modo longo**, mesmo se configurada no fluxo curto.
  Revise o resultado antes de publicar. O comportamento de publicação curto permanece.
- Reserve espaço para downloads, áudio, vídeo intermediário e capítulos finalizados.
  Eles não são removidos automaticamente. Memória de edição é limitada ao capítulo,
  mas tempo de CPU e uso de disco crescem com a duração e a quantidade de tarefas.
  Comece com uma tarefa por vez e duas threads de renderização.

## Testes locais

```sh
uv run --no-sync ruff check app cli.py main.py webui test
uv run --no-sync python -m compileall -q app cli.py main.py webui test
uv run --no-sync python -m pytest -q test
MPT_LONG_STRESS_TESTS=1 uv run --no-sync python -m pytest -q \
  test/services/test_long_video.py test/services/test_long_video_ffmpeg.py
```

Os testes cobrem contratos CLI/API, divisão sem perda de texto, dispatcher LLM,
isolamento e ordem de capítulos, etapas intermediárias, falhas e regressão do
modo curto. A integração renderiza dois capítulos curtos em 1920×1080 com
MoviePy/FFmpeg reais e narração sintética. Outro teste confere cores e frequências
de áudio antes/depois da junção. Os testes opcionais produzem contêineres sintéticos
de 10 e 30 minutos em baixa resolução e validam duração, faixas e decodificação.

Isso valida a mecânica local; **não equivale a um vídeo editorial de 30 minutos
renderizado em 1080p com provedores reais**. Qualidade de roteiro, latência,
quotas e qualidade de voz precisam ser avaliadas com as credenciais escolhidas.

### Resultado verificado em 30/08/2026

- macOS ARM64, Python 3.11.15, dependências de `uv.lock`, FFmpeg 8.1.1.
- Suíte completa com `MPT_LONG_STRESS_TESTS=1`: **866 passed, 11 skipped**,
  7.241 subtestes aprovados, 96,12 segundos. Os testes ignorados mantêm as
  condições de execução da suíte existente. Houve seis avisos de dependências
  e serialização de enums; nenhuma falha.
- Ruff, compilação Python e `git diff --check`: aprovados.
- Entrada real da CLI com roteiro fornecido e `--stop-at script`: saída JSON
  e manifesto de capítulos gerados, sem chamada de LLM/TTS/stock.
- Nenhuma chamada a serviço pago foi necessária para essa validação.
