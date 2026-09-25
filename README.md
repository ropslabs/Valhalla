# Valhalla – Funding-Graph & Wash-Detection

Kernmodul einer Signal-Engine, die **manipulierten von organischem On-Chain-Handel**
unterscheidet. Reine Logik über abstrakte Datenstrukturen: kein RPC, kein Indexer,
keine I/O. Daten rein, Scores raus. Ein späterer Indexer mappt echte Chain-Daten auf
die Typen in `valhalla/models.py`.

## Was das Modul kann – und was nicht

- **Es erkennt:** ob das Handelsvolumen eines Tokens von unabhängigen Akteuren kommt
  oder von wenigen, über Funding verbundenen Wallets, die hin und her handeln.
- **Es sagt NICHT voraus,** ob ein Projekt seriös ist. Ein Token mit 100 % organischem
  Volumen kann trotzdem ein Rug-Pull sein. Umgekehrt kann ein legitimes Projekt einen
  Market-Maker einsetzen.
- **Sybil-Resistenz ist ohne Identität nicht vollständig lösbar.** Ein Angreifer, der
  jede Wallet aus einer eigenen, unverbundenen externen Quelle finanziert (z. B. über
  getrennte CEX-Abhebungen), unterläuft das Cluster-Kriterium. Dann greift nur noch
  die Round-Trip-Regel pro Wallet. Dieses Verhalten ist als Test festgeschrieben
  (`test_known_limitation_hub_funded_sybils_evade_clustering`).
- **Ziel ist, die Kosten des Fälschens zu erhöhen, nicht Unfälschbarkeit.** Wer den
  Detektor umgehen will, muss Funding über Hubs verschleiern, Positionen länger als
  das Round-Trip-Fenster halten (Preisrisiko) und Kapital über viele Wallets verteilen.

## Datenmodell (`valhalla/models.py`)

| Typ | Felder | Bedeutung |
|---|---|---|
| `Transfer` | `from_wallet, to_wallet, amount, timestamp` | native Token-Bewegung = Funding (A→B: A hat B finanziert) |
| `Trade` | `wallet, token, side (buy/sell), amount, timestamp` | Swap; `amount` = Notional in der Quote-Währung |
| `Token` | `token, deployer=None` | optionales Deployer-Wallet pro Token |

Annahme: `Trade.amount` ist der Gegenwert in der Quote-Währung. Nur so sind Volumina
über Tokens vergleichbar und Kauf/Verkauf einer Wallet gegeneinander verrechenbar.
Preisbewegungen zwischen den Legs werden auf dieser Ebene ignoriert.

## Algorithmus

1. **Funding-Graph** (`graph.py`): networkx-`DiGraph`, eine Kante pro
   (Geldgeber, Empfänger), Dust-Transfers verworfen. Wallets mit sehr hohem Fan-out
   (CEX-Hot-Wallets, Bridges) oder explizit gelabelte gelten als **Hubs**. Sie
   verbinden nichts und werden nicht durchlaufen, sonst wären alle CEX-Kunden ein
   einziger Akteur.
2. **Clustering zu ökonomischen Akteuren** (`clustering.py`): Zwei Wallets gehören
   zusammen, wenn sie innerhalb von `max_hops` einen gemeinsamen Funding-Vorfahren
   haben. Die Wallet selbst zählt als Hop 0, direkte und gegenseitige Finanzierung
   sind also abgedeckt. Jede Kante wird gewichtet: 1.0, wenn das Funding nahe an der
   ersten Aktivität des Empfängers liegt, danach halbiert sich das Gewicht je
   Halbwertszeit. Verbunden wird, wenn das Produkt der beiden Pfadstärken
   `min_link_strength` erreicht. Die Verknüpfung ist transitiv (Zusammenhangskomponenten).
   Deployer werden mitgeclustert.
3. **Wash-Scoring pro Token** (`wash.py`): Auf einer DEX ist der Pool die Gegenpartei.
   „Volumen innerhalb eines Clusters“ heißt daher: derselbe Akteur kauft und verkauft
   kurz hintereinander. Gegenläufige Trades desselben Clusters werden FIFO innerhalb
   von `round_trip_window` verrechnet (Kauf→Verkauf wie Verkauf→Rückkauf). Jede
   verrechnete Einheit ist auf beiden Legs Wash:
   - `round_trip_volume`: beide Legs von derselben Wallet,
   - `intra_cluster_volume`: Legs von verschiedenen Wallets desselben Clusters.

   Nicht verrechnetes Volumen aus dem Deployer-Cluster ist `insider_volume`. Das ist
   keine Manipulation im engeren Sinn, aber auch keine organische Nachfrage.

### Output (`TokenReport`)

| Feld | Bedeutung |
|---|---|
| `raw_volume` | Summe aller Trades |
| `wash_volume`, `round_trip_volume`, `intra_cluster_volume` | erkanntes Wash-Volumen, aufgeschlüsselt |
| `insider_volume` | Rest-Volumen des Deployer-Clusters |
| `organic_volume` | Schätzung: `raw − wash − insider` |
| `wash_fraction` | `wash_volume / raw_volume` |
| `n_wallets` / `n_clusters` / `cluster_ratio` | rohe Wallet-Zahl vs. unabhängige Akteure |
| `net_new_buyers` | unabhängige Cluster (ohne Deployer) mit nennenswerter Netto-Long-Position |

## Token-Signale (`signals.py`)

Auf denselben Daten und **derselben Cluster-Zuordnung**. Alle Signale werden pro
Cluster gerechnet, nie pro Wallet: Sonst sähe ein Akteur mit vielen Wallets wie
breite, organische Beteiligung aus. Positionen ergeben sich aus den Trades als
Netto-Quote-Wert je Cluster (Käufe minus Verkäufe). „Nennenswert“ heißt überall:
über Dust und über `net_buyer_min_ratio` des Gekauften.

| Signal | Definition |
|---|---|
| `concentration.top_n_share` | Anteil der Top-N-Cluster an der Gesamtposition (keine Holder → 1.0) |
| `concentration.gini` | Gini über die Positionen der Holder-Cluster |
| `concentration.deployer_share` | Anteil des Deployer-Clusters; ist oben mitgezählt |
| `new_buyers.series` | je `signal_window`: DISTINCT Cluster, die erstmals mit nennenswerter Position aus dem Fenster gehen; jeder Cluster höchstens einmal, der Deployer-Cluster nie |
| `new_buyers.rate` | Summe pro `signal_window` seit dem ersten Trade (altersnormiert) |
| `retention.rate` | Anteil der Käufer-Cluster, die `retention_windows` Fenster nach ihrem ersten Kauf noch nennenswert halten; zu junge Cluster werden nicht bewertet |
| `organic_score` | `w_new · rate/(rate+scale) + w_ret · retention + w_conc · (1 − top_n_share) − w_wash · wash_fraction` |

`analyze_signals(transfers, trades, tokens, config, as_of=None)` liefert pro Token ein
`TokenSignals` mit allen Werten plus dem `TokenReport`. `as_of` ist standardmäßig der
letzte Trade über **alle** Tokens, damit alle Tokens gegen dieselbe Uhr altern.

**Befunde auf den synthetischen Daten (100 Seeds):**

- Stabil in der Reihenfolge ORGANIC > MIXED > WASH: `organic_score`, `new_buyers.rate`,
  `retention`. Der Score trennt ohne Überlappung.
- Konzentration trennt nur WASH von den anderen. Ein Wash-Bot endet netto flach und
  hält nichts, also erhöht er die Konzentration nicht. ORGANIC gegen MIXED ist hier
  nicht unterscheidbar; das wird bewusst nicht getestet.
- Der Gini ist zwischen den Szenarien nicht geordnet: Er misst Ungleichheit *unter* den
  Holdern, nicht deren Anzahl. Fünf gleich große Holder haben einen niedrigen Gini.
  Aussagekräftig ist er gegen Sybil-Splitting (siehe Test unten), für den Score zählt
  `top_n_share`.
- Spike-Käufer, die über eine Fenstergrenze halten, zählen als neue Käufer. Die
  Retention fängt sie ab.

Der entscheidende Test für die Cluster-Ebene ist
`test_sybil_split_position_is_detected_as_concentrated`: Ein Akteur verteilt 100
Einheiten auf 20 Wallets aus einer Quelle, dazu 20 Kleinanleger mit je 1. Auf
Cluster-Ebene: Top-5-Anteil ≥ 0.85, Gini ≥ 0.75. Dieselben Trades mit unabhängig
finanzierten Wallets ergeben Top-5 ≤ 0.25 und Gini ≤ 0.45.

## Tuning-Knöpfe (`DetectorConfig`)

| Parameter | Default | Wirkung |
|---|---|---|
| `dust_threshold` | 0.01 | kleinere Transfers ignorieren (gegen Dust-Spam) |
| `hub_min_fanout` | 50 | ab so vielen finanzierten Wallets gilt ein Funder als Hub |
| `known_hubs` | `{}` | extern gelabelte Infrastruktur-Wallets |
| `max_hops` | 2 | Tiefe der Suche nach gemeinsamen Vorfahren |
| `funding_window` | 3 Tage | Funding so nah an der ersten Aktivität zählt voll |
| `funding_half_life` | 7 Tage | Abklingen des Kantengewichts außerhalb des Fensters |
| `min_link_strength` | 0.25 | Mindest-Produkt der Pfadstärken für eine Verknüpfung |
| `round_trip_window` | 1 h | gegenläufige Trades innerhalb dieses Fensters = Wash |
| `net_buyer_min_ratio` | 0.1 | Mindest-Nettoposition (Anteil am Gekauften) für „neuer Käufer“ |
| `signal_window` | 1 Tag | Zeitfenster für Neu-Käufer-Serie, Altersnormierung, Retention |
| `concentration_top_n` | 10 | N für den Top-N-Anteil |
| `retention_windows` | 3 | so viele Fenster nach dem ersten Kauf wird Halten geprüft |
| `retention_prior` | 0.5 | Retention im Score, solange kein Käufer alt genug ist |
| `new_buyers_scale` | 10 | Neu-Käufer-Rate, bei der die Score-Komponente 0.5 ist |
| `weight_new_buyers` / `weight_retention` / `weight_concentration` / `weight_wash` | 0.3 / 0.3 / 0.2 / 0.2 | Score-Gewichte; Platzhalter bis zur Kalibrierung an gelabelten Echtdaten |

`hub_min_fanout` ist ein echter Zielkonflikt: Ein Operator, der mehr Wallets aus
einer Adresse finanziert, sieht aus wie ein Hub und fällt aus dem Clustering heraus.
Dafür zahlt er für entsprechend viele Wallets, und die Round-Trip-Regel greift weiter.

## Nutzung

```python
from valhalla import analyze, DetectorConfig, Transfer, Trade, Token

reports = analyze(transfers, trades, tokens=[Token("XYZ", deployer="0xdep")],
                  config=DetectorConfig(round_trip_window=2 * 3600))
reports["XYZ"].organic_volume, reports["XYZ"].wash_fraction
```

## Synthetische Daten & Tests

`valhalla/synthetic.py` erzeugt seeded und reproduzierbar drei Szenarien, jeweils mit
Ground Truth:

- **ORGANIC:** 200 unabhängige Wallets (eigene Quellen, teils CEX, wenige
  Freunde-und-Familie-Paare), gemischtes Kauf-/Halte-/Verkaufsverhalten.
- **WASH:** Market-Making-Bot. Ein Operator (vom Deployer finanziert) versorgt 6
  Wallets, die ständig hin und her handeln; dazu nur 5 echte Käufer und 15
  Spike-Käufer, die vom Schein-Volumen angelockt kaufen und nach 2–12 h komplett
  verkaufen.
- **MIXED:** organische Basis plus Bot-Overlay (Funding über eine Zwischen-Hop-Schicht)
  und 20 Spike-Käufer.

Die Spike-Käufer sind eine Modellannahme: Schein-Volumen zieht Momentum-Trader an,
die nicht halten. Die Trennung bei der Retention hängt an dieser Annahme; sie muss an
Echtdaten bestätigt werden.

`universe(seed)` legt alle drei in einen gemeinsamen Markt. Die Tests
(`tests/test_scenarios.py`, `tests/test_signals_scenarios.py`) prüfen über mehrere Seeds: Nach Rohvolumen liegt der
Wash-Token vorn, nach `organic_volume` klar hinten. Die `wash_fraction` ist in der
Reihenfolge ORGANIC < MIXED < WASH geordnet. Im gemischten Szenario werden organisches
und Wash-Volumen auf ±10–15 % genau getrennt.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

**Einordnung:** Die synthetischen Szenarien sind idealisiert. Der Bot handelt
innerhalb des Fensters, organische Trader verkaufen selten binnen einer Stunde.
Bestandene Tests belegen, dass die Logik das tut, was sie soll. Sie belegen nicht,
dass die Default-Schwellen für echte Chains richtig sind. Die müssen an gelabelten
Echtdaten kalibriert werden.

## Weitere bekannte Grenzen

- **Zeitgestrecktes Wash-Trading:** Positionen, die länger als `round_trip_window`
  gehalten werden, gelten als organisch. Ein größeres Fenster erzeugt mehr False
  Positives bei aktiven Tradern.
- **Wash über mehrere Cluster:** Zwei Operatoren, die sich gegenseitig Volumen
  zuschieben, ohne Funding-Verbindung, bleiben unerkannt.
- **Organische Scalper** innerhalb des Fensters werden als Round-Trip gezählt. Das ist
  gewollt (ohne Netto-Position kein Signal), senkt aber `organic_volume`.
- Das Clustering ist global über alle Tokens, das Scoring pro Token.
- Positionen stammen nur aus beobachteten Trades. Bestände aus Transfers, Airdrops
  oder aus der Zeit vor dem Beobachtungsfenster fehlen; negative Netto-Positionen
  werden ignoriert.
