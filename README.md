# SLATE — Simple Linux Agent Terminal Environment

SLATE è un ambiente GTK3 a tre colonne per lavorare con agenti software dentro
terminali persistenti via tmux e controllare automaticamente lo stato dei
repository Mercurial e Git presenti nei progetti.

## Requisiti

- Python 3.11 o successivo con PyGObject
- GTK 3, GtkSourceView 4, typelib Vte 2.91 e WebKitGTK 4.1
- tmux 3.x; Mercurial e/o Git in base ai repository usati
- Meld per i confronti grafici; TortoiseHg è opzionale e disponibile solo per HG

Su Ubuntu 24.04 i pacchetti principali sono `python3-gi`, `gir1.2-gtk-3.0`,
`gir1.2-gtksource-4`, `gir1.2-vte-2.91`, `gir1.2-webkit2-4.1`, `tmux`,
`mercurial`, `git`, `meld` e, se serve, `tortoisehg`.

## Installazione

Dal checkout dei sorgenti SLATE può essere avviato direttamente con
`./run-slate`. La procedura definitiva di clonazione e installazione verrà
completata dopo la creazione del progetto pubblico su GitHub.

## Aggiornamento e rimozione

Questo capitolo conterrà le procedure per aggiornare SLATE a una versione
successiva e per rimuovere applicazione e dati locali senza coinvolgere i
progetti o le sessioni tmux personali.

## Integrazione desktop

Questo capitolo descriverà l'eventuale installazione della voce nel menu
applicazioni, dell'icona e dei metadata desktop quando verrà scelto il formato
di distribuzione pubblico.

## Avvio

```console
./run-slate
```

L'avvio normale si separa completamente dal terminale e restituisce subito il
prompt. Il processo GTK non conserva terminale, sessione o directory corrente
del launcher.

Nei terminali integrati, `Ctrl+Shift+V` o `Shift+Insert` incollano e
`Ctrl+Shift+C` o `Ctrl+Insert` copiano. Il menu contestuale offre le stesse
azioni; `Ctrl+C` senza Shift resta riservato all'interruzione del processo.
Il pulsante “Codex” crea un nuovo terminale persistente `codex-N` e vi esegue
`codex resume`; uscendo da Codex si ritorna alla shell dello stesso terminale.
SLATE salva il comando insieme al terminale: se dopo un riavvio del sistema la
sessione tmux non esiste più, la ricrea ed esegue nuovamente `codex resume` alla
prima selezione. Se tmux è ancora vivo, si limita invece a riagganciarsi senza
reinviare il comando.
I terminali salvati da versioni precedenti restano terminali normali: SLATE non
deduce né migra comandi a partire dai nomi `main` o `term-N`.
“Esegui” apre un campo per eseguire e memorizzare una riga shell
personalizzata. Il terminale prende il nome dal primo eseguibile (`ssh-1`,
`npm-1`, ecc.) e segue lo stesso ripristino post-reboot dei terminali Codex.
Se si esegue `exit` nella shell, la sessione conclusa viene rimossa
automaticamente dall'albero invece di lasciare una vista terminale “exited”.
F2 e “Rinomina” usano un popup con campo di testo; la rinomina inline nell'albero
non viene utilizzata.

Un secondo avvio normale presenta l'istanza esistente. Soltanto il parametro
esplicito `--agent-debug` permette una seconda GUI, marcata "AGENT DEBUG", con
config temporanea, application ID e socket tmux separati. Questa modalità resta
intenzionalmente in primo piano per mostrare diagnostica ed errori:

```console
./run-slate --agent-debug
```

Per diagnosticare l'istanza di produzione senza cambiare config, application ID
o socket tmux, usare `--debug`. Questo flag disabilita soltanto il detach,
mantiene errori e traceback collegati al terminale e abilita gli stack dei
segnali fatali:

```console
./run-slate --debug
```

La modalità debug stampa anche le navigazioni bloccate da WebKit con l'URL
completo. Query string e frammenti possono contenere nonce, token o altri dati
sensibili: l'output del terminale non va pubblicato senza averlo controllato.

Il controllo attività tmux parte soltanto con la finestra in primo piano e con
almeno un terminale caricato: usa 100 ms di debounce e riparte 5 secondi dopo la
conclusione del controllo precedente. I terminali configurati vengono caricati
solo alla prima selezione.

La sola configurazione viene salvata in
`~/.config/slate/config.json`. SLATE non cerca, migra o modifica configurazioni
con nomi precedenti e non crea file nei progetti.

## Revisioni, anteprima e azioni SCM

SLATE scopre repository HG e Git normali nella root o nelle sottocartelle del
progetto. Ogni repository ha un'icona specifica; Git worktree e submodule con
`.git` in forma di file non sono gestiti. Un click su un file modificato mostra
il diff colorato sopra le prime due colonne. I file nuovi e aggiunti mostrano il contenuto corrente
completo; i file rimossi mostrano il contenuto completo della revisione base,
sempre con syntax highlighting. Gli spostamenti riconosciuti dall'SCM
compaiono come un'unica riga `origine → destinazione` nel gruppo **Spostati**;
l'anteprima usa il diff di rinomina e commit o ripristino includono entrambi i
path. L'anteprima si chiude con Esc o cliccando fuori.
Il menu contestuale permette di visualizzare il file con l'applicazione desktop
predefinita (`xdg-open`), modificarlo in una nuova finestra gVim, aggiungere i
file nuovi o eliminare il file dopo una conferma. Con la riga evidenziata sono
disponibili anche `V` (visualizza), `E` (modifica), `A` (aggiungi un file nuovo),
`Canc` (elimina con conferma) e `Spazio` (uniforma le checkbox dei file tracciati
evidenziati in base alla riga col cursore). Le
frecce Su/Giù cambiano riga e aggiornano immediatamente l'anteprima.
Gli spostamenti del cursore causati internamente da un refresh, incluso quello
dopo un commit, non aprono automaticamente la preview del file successivo.
Ctrl e Shift permettono di evidenziare più file nuovi; “Aggiungi” e `A` agiscono
soltanto sui nuovi evidenziati e avviano direttamente l'aggiunta esplicita,
senza conferma.
Quando sono evidenziati più file, il menu contestuale offre anche l'azione con
scorciatoia `Spazio` per uniformare le checkbox dei file tracciati presenti;
le azioni Visualizza, Modifica ed Elimina, valide per un solo file, non vengono
mostrate e le relative scorciatoie restano inattive.
Il Commit parte direttamente dal pulsante, senza un secondo dialogo, dopo che
messaggio e checkbox lo hanno reso disponibile. Per Git la UI non espone due
sezioni staged/unstaged: i file tracciati modificati sono selezionabili come in
Mercurial, i file nuovi richiedono **Aggiungi**, e il commit usa soltanto i path
selezionati senza eseguire automaticamente `git add`.

Il commit rimane disattivato finché il messaggio è vuoto e usa il normale stile
neutro del tema GTK. Aggiungi nuovi e Ripristina selezionati sono nella riga
sopra il messaggio; l'ultima riga contiene “Tutti i file” e Commit. Meld è nel
menu contestuale del repository; TortoiseHg compare soltanto sui repository HG.
Checkbox esplicite sui file tracciati determinano sia i target del commit sia
quelli del ripristino. Restano nella colonna gerarchica con expander compatto;
“Seleziona tutti” e “Tutti i file” sono sincronizzati e selezionano soltanto i
file tracciati.
Su una working copy pulita il pannello mantiene visibili campo e azioni: quelle
non applicabili restano disabilitate e il nodo del repository resta disponibile.

Il menu della radice del repository separa gli strumenti di ispezione dalle
operazioni **Aggiorna**, **Pubblica**, **Nuovo branch**, **Passa a branch**,
**Merge branch** e **Assegna tag**. Ogni operazione usa una modale dedicata e,
se incontra una situazione ambigua o non supportata, mostra il problema senza
tentare force, rebase o correzioni automatiche.

**Aggiorna** accetta soltanto avanzamenti lineari: una singola head per HG e
fast-forward per Git. **Pubblica** usa soltanto la destinazione già configurata
e il normale push. Nei repository intenzionalmente locali entrambe le azioni
terminano con un messaggio informativo, senza considerare l'assenza del remote
un errore e senza modificare la configurazione.

Le azioni sui branch lavorano soltanto con branch locali. Cambio branch, merge
e tag richiedono una working copy tracciata pulita; la creazione di un branch
mantiene invece le modifiche locali. Head HG multiple, divergenze Git con
l'upstream e collisioni sui file restano da gestire manualmente.

Il merge fra branch locali non crea automaticamente il commit. In presenza di
conflitti la modale offre soltanto **Apri in Meld**; conclusione o annullamento
del merge restano operazioni manuali.

## File del progetto

Il selettore `Revisioni | File` sostituisce il contenuto della terza colonna
senza ridurre lo spazio del terminale. La vista File carica le directory in modo
asincrono e un click su un file usa la stessa anteprima read-only sopra le prime
due colonne. `V`, `E` e `Canc` riusano rispettivamente visualizzazione desktop,
modifica con gVim e cancellazione confermata; le directory non possono essere
cancellate.

Dal menu principale, **Impostazioni** apre le sezioni Revisioni, File ed Editor.
Le rispettive dimensioni del testo si regolano separatamente, vengono applicate
subito e restano salvate nella configurazione unica di SLATE.

## Editor interno

Dal menu contestuale di un file, **Modifica in SLATE** (`M`) aggiunge una voce
file sotto il progetto, allo stesso livello dei terminali. Selezionandola, la
colonna centrale mostra il relativo GtkSourceView senza alcuna barra di tab;
**Modifica in gVim** (`E`) conserva l'editor esterno. Le voci possono appartenere
a progetti diversi, seguono insieme ai terminali l'ordine cronologico di
creazione e restano configurate fra i riavvii, mentre i contenuti non salvati non
vengono trasformati in bozze.

L'editor offre syntax highlighting, numeri di riga, undo/redo, ricerca con
`Ctrl+F`, vai alla riga con `Ctrl+G` e salvataggio atomico con `Ctrl+S`. Le
modifiche esterne ricaricano i buffer puliti; sui buffer dirty una campanella e
un avviso interno impongono la scelta fra versione su disco e versione locale.
La sezione **Editor** nelle Impostazioni controlla separatamente il suo font.

## Browser interno

**Apri URL** aggiunge sotto il progetto attivo una pagina WebKitGTK che
rimane viva passando fra terminali, editor e progetti. La toolbar offre
Indietro, Avanti, Stop/Ricarica, barra URL, indicatore di caricamento e accesso
ai DevTools. `F12` o `Ctrl+Shift+I` aprono il Web Inspector con console
JavaScript, DOM, stili, sorgenti e richieste di rete.

Le pagine normali conservano URL, titolo e posizione nell'albero e vengono
ripristinate come righe lazy: WebKit e la rete partono soltanto alla prima
selezione. Cookie, local storage, IndexedDB, cache e service worker condividono
il profilo globale in `$XDG_DATA_HOME/slate/webkit` e
`$XDG_CACHE_HOME/slate/webkit`, senza scrivere nelle working copy.

**Incognito** crea invece un contesto effimero e isolato per ogni
pressione. La tab, la posizione e l'ultima URL entrano nel JSON e ricompaiono
lazy al riavvio, ma cookie e storage anonimi vengono eliminati: alla prima
selezione la tab riparte con un nuovo contesto effimero. I link che richiedono
una nuova finestra, incluso `target="_blank"`, si aprono nel browser di sistema
senza aggiungere item al progetto. I form che preparano prima una finestra vuota
nominata, come la preview di WordPress, completano il POST in una WebView
temporanea invisibile e aprono poi l'URL finale nel browser di sistema.
Le scorciatoie browser comprendono `Ctrl+L`, `Alt+←/→`, `Ctrl+R`, `F5`, `Esc`,
`Ctrl+W`, `F12` e `Ctrl+Shift+I`.

La toolbar include anche il menu **Responsive**. Scegliendo un dispositivo,
la pagina mostra misura e percentuale di scala e viene centrata e ridotta
automaticamente quando non entra. La voce iniziale disattiva l’anteprima e al
riavvio nessun preset è selezionato. Il pulsante **× Desktop** nella cornice o
`Esc` disattivano rapidamente la preview. Non vengono emulati DPR, touch o
user-agent.

“Espandi” attraversa l'albero visibile ma si ferma sulle directory escluse
pesanti, che si possono comunque aprire manualmente. I toggle “Nascosti” ed
“Esclusi”, insieme alle directory espanse, vengono ricordati per progetto.
I metadati `.hg`, `.git` e `.svn` non vengono mai mostrati.

## Persistenza tmux

Le sessioni usano esclusivamente il server `tmux -L slate`. Sopravvivono a un
crash della GUI, ma non a reboot o logout che termini i processi utente. Se la
distribuzione usa `KillUserProcesses=yes`, valutare consapevolmente:

```console
loginctl enable-linger "$USER"
```

Se SLATE non si avvia, le sessioni rimaste attive possono essere elencate e
raggiunte manualmente dal terminale:

```console
tmux -L slate list-sessions
tmux -L slate attach-session -t NOME_SESSIONE
```

Per esempio, per collegarsi alla sessione `miosito--main`:

```console
tmux -L slate attach-session -t miosito--main
```

Per scollegarsi senza terminare la sessione o i processi in esecuzione, premere
`Ctrl+B` e poi `D`. Il messaggio `no server running` indica che sul socket
normale di SLATE non risultano sessioni attive. Le istanze lanciate con
`--agent-debug` usano invece un socket tmux temporaneo e distinto.

All'uscita pulita, i processi attivi vengono elencati e si può annullare,
lasciarli in background oppure terminarli. Le sessioni personali sul server tmux
predefinito non vengono mai toccate.

## Test

```console
./tests/run-final-checks.sh
```

Il controllo esegue test unitari e una prova d'integrazione del watcher su un
repository Mercurial temporaneo. Non apre finestre GTK: la verifica manuale
della GUI va eseguita separatamente perché il window manager può assegnarle il
focus anche quando l'applicazione tenta di evitarlo.

## Licenza

SLATE è software libero distribuito secondo i termini della GNU General Public
License, versione 2 o, a scelta, una versione successiva. Il testo completo è
disponibile nel file [`LICENSE`](LICENSE).

Le icone dei sistemi di controllo versione mantengono le rispettive licenze e
attribuzioni:

- il logomark Git è opera di Jason Long ed è distribuito con licenza
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/);
- il logo “droplets” di Mercurial è opera di Cali Mastny e Matt Mackall ed è
  distribuito con licenza GPLv2+.

L'icona **Incognito** è un disegno originale di SLATE distribuito con licenza
GPLv2+. Il pulsante **Codex** usa il Blossom in scala di grigi per identificare
direttamente il servizio OpenAI che avvia; OpenAI e i relativi elementi grafici
sono marchi di OpenAI.

Git e il logo Git sono marchi registrati o marchi di Software Freedom
Conservancy, Inc., organizzazione che ospita il progetto Git.
