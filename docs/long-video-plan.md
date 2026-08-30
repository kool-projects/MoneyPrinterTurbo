# Plano técnico — MVP de vídeos longos

Base inspecionada: `6c15b42bd4435d28b280225d21f47a057a274b81`.
Rota inicial: `vibe trilha` executado no projeto de referência; stack confirmada
pela leitura: Python, FastAPI, Streamlit, MoviePy, FFmpeg e pytest.

## Decisão

Adicionar um ramo opt-in ao pipeline existente. `target_duration_minutes=0`
preserva o comportamento atual; valores inteiros de 10 a 30 ativam o MVP em
16:9, com uma saída por tarefa. Nenhum provedor será substituído.
Não é necessário banco novo, fila nova, novo frontend ou dependência nova.

## Arquivos exatos

1. `app/models/schema.py`: campo e validação de duração; documentar a restrição
   de uma saída e áudio sintetizado no modo longo.
2. `app/services/llm.py`: outline e roteiro por capítulo usando o dispatcher
   atual, idioma e prompts existentes; orçamento aproximado de narração.
3. `app/services/long_video.py` (novo): divisão sem perda de texto, manifesto
   por capítulo, TTS/legenda/mídia/render sequenciais reutilizando helpers de
   `task.py`; progresso e falhas identificam capítulo e etapa.
4. `app/services/task.py`: encaminhar o modo longo após preflight comum;
   permitir mapear progresso de renderização do capítulo para a tarefa pai.
5. `app/services/video.py`: concatenação final com FFmpeg, vídeo e áudio
   explícitos, cópia de streams, saída temporária e substituição atômica.
6. `cli.py`: `--target-duration-minutes`; JSON de lote e API herdam o modelo.
7. `test/services/test_long_video.py` (novo): validação, geração/divisão,
   isolamento de capítulos, falhas, compatibilidade do fluxo curto e CLI.
8. `test/services/test_long_video_ffmpeg.py` (novo): integração offline com
   áudio/vídeo sintéticos, duração, ordem e concatenação real.
9. `docs/long-video.md` (novo) e `README-en.md`: execução por CLI/API, roteiro
   fornecido, resultados, limites, custos externos e comandos de teste.

## Ordem e aceitação

Contrato → roteiro/divisão → orquestração → concatenação → entradas → testes
→ documentação. A interface existente continua disponível; o controle novo
fica inicialmente em CLI/API para minimizar mudanças.

- Nenhuma chamada externa nos testes: provedores simulados, FFmpeg real.
- Testar capítulos pequenos pelo pipeline real e concatenação sintética
  com duração longa, além dos testes de regressão existentes.
- Duração é uma meta de roteiro; a duração real vem do áudio. Avisar desvios,
  sem cortar narração nem repetir texto para atingir o número informado.
- Não publicar capítulos automaticamente. O MVP longo retorna os arquivos
  para revisão; a publicação automática de vídeos curtos permanece intacta.
- Áudio único enviado pelo usuário e LoomLoom com orçamento confirmado são
  recusados antecipadamente no modo longo: exigiriam alinhamento de áudio ou
  novas confirmações de custo por capítulo. Continuam disponíveis no modo curto.
- Renderização sequencial limita memória ao capítulo. Manter artefatos para
  diagnóstico; retomada automática e continuidade de música ficam fora do MVP.
