# SLATE — Simple Linux Agent Terminal Environment

[English](README.md) | Italiano

La documentazione inglese è la versione di riferimento del progetto.
L'interfaccia del programma usa etichette in inglese, riportate in grassetto
anche in questa guida.

SLATE riunisce progetti, terminali persistenti, modifiche Git/Mercurial, file,
editor e pagine Web in un'unica applicazione GTK 3 per Linux.

È pensato per chi lavora contemporaneamente su più progetti e vuole ritrovare
terminali e strumenti nello stesso posto, senza creare file di stato nelle
directory di lavoro.

## Cosa offre

- un elenco ordinato di progetti e relativi terminali;
- terminali persistenti tramite un server tmux dedicato;
- avvio rapido di Codex o di qualsiasi comando shell;
- stato e operazioni essenziali per repository Git e Mercurial;
- esplorazione dei file con anteprima e azioni contestuali;
- editor GtkSourceView integrato;
- browser WebKitGTK con modalità normale, incognito e anteprima responsive;
- caricamento su richiesta: all'avvio non vengono aperti terminali, editor o
  pagine Web finché non vengono selezionati.

## Installazione rapida

### Requisiti

SLATE richiede:

- Linux con ambiente desktop e sessione grafica;
- Python 3.11 o successivo con PyGObject;
- GTK 3, GtkSourceView 4, Vte 2.91 e WebKitGTK 4.1;
- tmux 3.x;
- Git per clonare SLATE e gestire repository Git.

#### Debian, Ubuntu e Linux Mint

Su Debian 12 o successivo, Ubuntu 24.04 o successivo e Linux Mint 22 o
successivo, installare i componenti indispensabili con:

```console
sudo apt update
sudo apt install git gir1.2-gtk-3.0 gir1.2-gtksource-4 \
    gir1.2-vte-2.91 gir1.2-webkit2-4.1 python3 python3-gi tmux
```

#### Fedora

Installare i componenti indispensabili con:

```console
sudo dnf install git gtk3 gtksourceview4 python3 python3-gobject \
    tmux vte291 webkit2gtk4.1
```

#### Arch Linux

Aggiornare il sistema e installare i componenti indispensabili dai repository
ufficiali con:

```console
sudo pacman -Syu --needed git gtk3 gtksourceview4 python python-gobject \
    tmux vte3 webkit2gtk-4.1
```

### Strumenti opzionali

Questi strumenti abilitano funzionalità aggiuntive ma non sono necessari per
avviare SLATE. Su Debian, Ubuntu e Linux Mint, installarli con:

```console
sudo apt install meld mercurial ripgrep tortoisehg vim-gtk3 xdg-utils
```

Su Fedora, installarli con:

```console
sudo dnf install meld mercurial ripgrep tortoisehg vim-X11 xdg-utils
```

Su Arch Linux, installare quelli disponibili nei repository ufficiali con:

```console
sudo pacman -S --needed gvim meld mercurial ripgrep xdg-utils
```

TortoiseHg è disponibile separatamente nell'Arch User Repository (AUR).

| Strumento   | Funzionalità                                      |
|-------------|---------------------------------------------------|
| Meld        | Confronti grafici                                 |
| Mercurial   | Repository Mercurial                              |
| TortoiseHg  | Interfaccia TortoiseHg per repository Mercurial   |
| gVim        | Modifica esterna con gVim                         |
| ripgrep     | Ricerca nel contenuto dei file                    |
| `xdg-open`  | Apertura con l'applicazione desktop predefinita   |

Il pulsante **Codex** richiede la
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli), già installata e
autenticata. Tutte le altre funzioni di SLATE restano utilizzabili senza Codex.

### Clone e avvio

```console
git clone https://github.com/badpenguin/slate.git
cd slate
./run-slate
```

Non è necessario eseguire `pip install`: il checkout contiene il codice Python
dell'applicazione e le librerie GTK provengono dai pacchetti di sistema.

## Primo utilizzo

1. Premere **New Project** e scegliere una directory esistente.
2. Selezionare il progetto nella colonna sinistra.
3. Premere **New Terminal** per creare il primo terminale del progetto.
4. Usare **Codex** per aprire `codex resume` oppure **Execute** per avviare e
   memorizzare un comando personalizzato.

SLATE associa ogni terminale al progetto selezionato. Passare a un altro
progetto non interrompe i processi e, tornando indietro, viene riutilizzato lo
stesso terminale.

## Interfaccia

La finestra è divisa in tre colonne:

1. **Projects**: progetti, terminali, editor e pagine Web aperte.
2. **Workspace**: terminale, editor o browser selezionato.
3. **Revisions/Files**: modifiche dei repository oppure albero dei file.

```text
New Project | New Terminal | Execute ▾ | Codex | Open URL | Incognito
┌────────────────────────┬──────────────────────────────────────────┬────────────────────────────┐
│ PROJECTS               │ WORKSPACE                                │ CHANGES | FILES            │
├────────────────────────┼──────────────────────────────────────────┼────────────────────────────┤
│ ▾ demo-project         │ $ npm run dev                            │ Git · main                 │
│   terminal-1           │ Starting the development server...       │                            │
│   codex-1              │                                          │ Modified                   │
│   src/app.py           │ The selected terminal, editor, or        │   [×] src/app.py           │
│   Web Page             │ browser occupies this column.            │                            │
│                        │                                          │ New                        │
│ ▸ second-project       │ Switching items does not interrupt       │   [ ] tests/test_app.py    │
│                        │ processes in other terminals.            │                            │
│                        │                                          │ Commit message             │
│                        │                                          │ [Select all] [Commit]      │
└────────────────────────┴──────────────────────────────────────────┴────────────────────────────┘
```

Il progetto attivo conserva separatamente terminali, file aperti e pagine Web.
Le dimensioni del testo di terminale, Revisioni, File ed Editor sono regolabili
da **Settings**.

## Terminali

### Creazione e persistenza

**New Terminal** crea una shell persistente. **Execute** accetta una riga di
comando e assegna automaticamente un nome come `ssh-1` o `npm-1`. **Codex** crea
un terminale `codex-N` e avvia `codex resume`.

La sezione **Commands** di **Settings** definisce launcher globali riutilizzabili.
Ogni launcher ha una label, un comando shell e un'icona scelta fra quelle
disponibili nel tema GTK corrente. Se esiste almeno un launcher, la piccola
freccia accanto a **Execute** li apre come pulsanti nell'ordine configurato. La
scelta crea un nuovo terminale persistente nel progetto attivo; senza launcher
configurati, la freccia non viene mostrata. I terminali esistenti mantengono
l'icona scelta anche se il launcher viene poi modificato o rimosso.

SLATE ricorda anche il comando associato al terminale. Se la sessione tmux è
ancora attiva, si limita a riagganciarla; se non esiste più, la ricrea alla
prima selezione. Eseguendo `exit`, il terminale concluso viene rimosso
dall'elenco.

Un terminale può essere rinominato con **Rename** o `F2`.

### Scorciatoie

| Azione                 | Scorciatoie                          |
|------------------------|--------------------------------------|
| Copia                  | `Ctrl+Shift+C` oppure `Ctrl+Insert`  |
| Incolla                | `Ctrl+Shift+V` oppure `Shift+Insert` |
| Interrompi il processo | `Ctrl+C`                             |
| Rinomina il terminale  | `F2`                                 |

`Ctrl+C` senza Shift raggiunge sempre il processo nel terminale.

## Revisioni Git e Mercurial

La scheda **Revisions** rileva i repository Git e Mercurial presenti nella
directory del progetto e nelle sue sottodirectory. Lo stato viene aggiornato
automaticamente quando cambiano file o metadati del repository.

Selezionando un file modificato viene mostrata un'anteprima del diff. I file
nuovi mostrano il contenuto corrente; quelli rimossi mostrano la versione base;
gli spostamenti riconosciuti appaiono come `origine → destinazione`.

### Commit e ripristino

- i file tracciati si includono nel commit tramite le checkbox;
- i file nuovi devono essere aggiunti esplicitamente con **Add**;
- **Select all** seleziona tutti i file tracciati;
- il commit diventa disponibile dopo aver scritto il messaggio;
- `Ctrl+Enter` esegue il commit;
- il ripristino e l'eliminazione richiedono conferma.

La vista Git è intenzionalmente simile a quella Mercurial: non separa staged e
unstaged e non esegue automaticamente `git add` prima del commit.

### Operazioni repository

Dal menu contestuale della radice del repository sono disponibili:

- **Verify**;
- **Update**;
- **Publish**;
- **New branch**;
- **Switch branch**;
- **Merge branch**;
- **Assign tag**;
- apertura in Meld e, per Mercurial, in TortoiseHg.

SLATE usa soltanto remote e upstream già configurati. Non configura
automaticamente destinazioni, non esegue force push o rebase e non crea commit
automatici durante i merge. Conflitti, divergenze e situazioni ambigue vengono
segnalati e lasciati alla gestione esplicita dell'utente.

Lo stato remoto viene verificato senza dialog una volta alla prima attivazione
del progetto e nuovamente dopo ogni **Scan** manuale. I controlli restano lazy
sul solo progetto attivo, procedono un repository alla volta e non richiedono
mai credenziali; possono soltanto riutilizzare quelle già disponibili nella
cache Git in memoria o in un agente SSH. **Verify** resta disponibile per un
nuovo tentativo esplicito.

Durante **Publish**, Git richiede automaticamente le credenziali necessarie per
un remote HTTPS. SLATE mostra una finestra per utente e password o token e usa i
valori tramite la cache Git esclusivamente in memoria per un massimo di 60 minuti.
Le credenziali non vengono mai scritte nella configurazione di SLATE, nel
repository o in un gestore di credenziali persistente. GitHub richiede un token
personale al posto della password dell'account.

Git worktree e submodule con `.git` in forma di file non sono supportati.

### Azioni rapide sui file

| Azione                                      | Scorciatoia |
|---------------------------------------------|-------------|
| Visualizza con l'applicazione predefinita   | `V`         |
| Modifica nell'editor interno                | `E`         |
| Modifica con gVim                           | `M`         |
| Aggiungi un file nuovo                      | `A`         |
| Elimina con conferma                        | `Delete`    |
| Uniforma le checkbox dei file evidenziati   | `Space`     |

## File ed editor

La scheda **File** mostra l'albero del progetto. Le directory vengono caricate
su richiesta e mantengono espansione e posizione passando fra progetti.

- **+ File** crea un file;
- il menu contestuale permette di creare directory, rinominare, eliminare e
  aprire un terminale nella posizione scelta;
- **Hidden** mostra i file nascosti;
- **Excluded** mostra i file ignorati dal repository;
- **Expand** apre ricorsivamente l'albero, fermandosi sulle directory escluse
  più pesanti.

I metadati `.git`, `.hg` e `.svn` non vengono mai mostrati. Link simbolici e
percorsi che escono dalla directory del progetto non possono essere usati per
leggere, modificare o eliminare file esterni.

**Edit in SLATE** apre il file nella colonna centrale con evidenziazione
sintattica, numeri di riga, ricerca, annullamento/ripristino e salvataggio
atomico. Se il file cambia anche sul disco, SLATE chiede quale versione
mantenere invece di sovrascriverla automaticamente.

L'editor supporta file testuali UTF-8 e non conserva bozze dei contenuti non
salvati.

### Ricerca nel progetto

Il pulsante **Project Search** o `Ctrl+Shift+F` apre una ricerca nel contenuto dei file
del progetto attivo. La ricerca parte automaticamente da quattro caratteri,
usa un confronto letterale smart-case e rispetta file ignorati, directory
escluse e limite testuale di 5 MiB.

La lista mostra a sinistra il testo corrispondente e a destra il percorso con
numero di riga; spostando la selezione, l'anteprima read-only centra la riga nel relativo contesto. Sono
mostrati al massimo 100 risultati. Dal menu contestuale o con `V`, `E`, `M` e
`D` è possibile rispettivamente aprire il file, modificarlo nell'editor interno
di SLATE, modificarlo con l'editor esterno configurato o visualizzarne la patch
disponibile in Meld. `Esc` chiude la ricerca.

Se `rg` non è installato, SLATE continua ad avviarsi normalmente e disabilita
soltanto questa funzione.

## Browser

**Open URL** aggiunge una pagina Web al progetto. Le pagine normali mantengono
URL, titolo e posizione e vengono caricate soltanto alla prima selezione.

**Incognito** crea un contesto effimero e isolato per ogni pagina. SLATE ricorda
la presenza della scheda e l'ultimo URL, ma elimina cookie e storage anonimi; al
riavvio viene creato un nuovo contesto incognito.

Il menu **Responsive** centra la pagina dentro dimensioni predefinite e la
riduce quando non entra nello spazio disponibile. Non emula touch, DPR o
user-agent. `F12` o `Ctrl+Shift+I` aprono gli strumenti di sviluppo WebKit.

| Azione                         | Scorciatoia                 |
|--------------------------------|-----------------------------|
| Attiva la barra URL            | `Ctrl+L`                    |
| Indietro/Avanti                | `Alt+←` / `Alt+→`           |
| Ricarica                       | `Ctrl+R` oppure `F5`        |
| Interrompi                     | `Esc`                       |
| Chiudi la pagina               | `Ctrl+W`                    |
| Apri gli strumenti di sviluppo | `F12` oppure `Ctrl+Shift+I` |

## Dati e sessioni tmux

La configurazione è salvata in:

```text
~/.config/slate/config.json
```

SLATE non scrive configurazioni, cache o file di stato nelle directory dei
progetti.

Le pagine Web normali condividono il profilo presente in:

```text
$XDG_DATA_HOME/slate/webkit
$XDG_CACHE_HOME/slate/webkit
```

I terminali usano esclusivamente il server `tmux -L slate`, separato dal server
tmux personale. Le sessioni sopravvivono a un crash della GUI, ma non a un
riavvio o a un logout che termini i processi utente.

Le sessioni ancora attive possono essere elencate e raggiunte manualmente:

```console
tmux -L slate list-sessions
tmux -L slate attach-session -t NOME_SESSIONE
```

Per scollegarsi da tmux senza terminare la sessione, premere `Ctrl+B` e poi `D`.

Sui sistemi configurati con `KillUserProcesses=yes`, la persistenza oltre il
logout può richiedere:

```console
loginctl enable-linger "$USER"
```

Valutare questa impostazione consapevolmente perché riguarda l'intera sessione
utente, non soltanto SLATE.

## Diagnostica

Per mantenere SLATE collegato al terminale e visualizzare errori e traceback:

```console
./run-slate --debug
```

La modalità debug usa configurazione e sessioni di produzione. Può inoltre
stampare URL completi: controllare l'output prima di condividerlo perché query e
frammenti possono contenere token o altri dati sensibili.

Per avviare un'istanza isolata con configurazione, ID applicazione e socket tmux
temporanei:

```console
./run-slate --agent-debug
```

Un secondo avvio normale non crea una nuova finestra: presenta l'istanza già
attiva.

## Test

La suite automatica non apre finestre GTK:

```console
./tests/run-final-checks.sh
```

La verifica manuale dell'interfaccia resta separata perché il window manager
può assegnare brevemente il focus a una finestra di test.

## Segnalazioni e contributi

Bug e proposte possono essere aperti nelle
[issue di GitHub](https://github.com/badpenguin/slate/issues). Indicare la
distribuzione Linux, la versione di SLATE, i passaggi per riprodurre il problema
e gli eventuali messaggi di diagnostica dopo aver rimosso dati sensibili.

## Licenza

- **SLATE** è software libero distribuito secondo i termini della GNU General
  Public License, versione 2 o, a scelta, una versione successiva. Il testo
  completo è disponibile nel file [`LICENSE`](LICENSE).
- Il **logomark Git** è opera di Jason Long ed è distribuito con licenza
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
- Il logo **“droplets” di Mercurial** è opera di Cali Mastny e Matt Mackall ed è
  distribuito con licenza GPLv2+.
- L'icona **Incognito** è un disegno originale di SLATE distribuito con licenza
  GPLv2+.
- Il pulsante **Codex** usa il Blossom in scala di grigi per identificare il
  servizio OpenAI che avvia; OpenAI e i relativi elementi grafici sono marchi
  di OpenAI.
- **Git e il logo Git** sono marchi registrati o marchi di Software Freedom
  Conservancy, Inc., organizzazione che ospita il progetto Git.
