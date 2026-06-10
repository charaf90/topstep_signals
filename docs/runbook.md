# Runbook — Topstep Signals (édition solo)

> **Quand utiliser ce document.** En cas d'incident pendant que le bot tourne
> ou de doute sur l'état du système. Chaque scénario suit le format
> Symptômes → Action → Vérification → Récupération. Les commandes sont
> copier-collables tel quel depuis `/home/charaf/topstep_signals/`.

---

## 🚨 Accès rapide

| # | Scénario | Lien |
|---|---|---|
| 1 | Bot poste des ordres en boucle | [↓](#1-bot-poste-des-ordres-en-boucle) |
| 2 | WebSocket déconnecté > 5 min | [↓](#2-websocket-déconnecté--5-min) |
| 3 | État interne désynchronisé du broker | [↓](#3-état-interne-désynchronisé-du-broker) |
| 4 | Limite Topstep approchée (< $200) | [↓](#4-limite-topstep-approchée--200) |
| 5 | Arrêt propre du daemon en urgence | [↓](#5-arrêt-propre-du-daemon-en-urgence) |
| 6 | Flatten toutes positions immédiatement | [↓](#6-flatten-toutes-positions-immédiatement) |
| 7 | Isoler un ticker problématique | [↓](#7-isoler-un-ticker-problématique) |
| 8 | Redémarrer après crash | [↓](#8-redémarrer-après-crash) |
| ⚠️ | Sécurité Tailscale (anti-VPN sur Topstep) | [↓](#-encart-sécurité-tailscale) |

---

## Checklist d'inspection rapide (à exécuter quand on doute)

```bash
# Le daemon tourne-t-il ?
tmux ls

# Dernier événement live
tail -n 20 logs/trading_events.log

# Snapshot risk manager
cat state/live_state.json | python -m json.tool | head -40

# Réconciliation broker ↔ state (lecture seule)
python scripts/reconcile_daily.py
```

Si l'un des trois piliers (tmux / logs / state) est anormal, suivre le scénario adapté.

---

## 1. Bot poste des ordres en boucle

**Symptômes**
- `tail -f logs/trading_events.log` montre des lignes `[ORDRE]` à un rythme
  anormal (> 5 ordres / 30s sur un même ticker).
- Plusieurs `[ORDRE]` annulés `[ANNULÉ]` puis re-placés en série rapide.
- Telegram envoie des notifications en rafale.

**Action**
```bash
# 1. STOP immédiat — kill du daemon
tmux kill-session -t topstep

# 2. Vérifier qu'aucun process zombie ne traîne
ps aux | grep -E "live_runner|live\.py" | grep -v grep

# 3. Si zombie présent, kill PID
# kill -9 <PID>
```

**Vérification**
```bash
# 4. Côté broker : annuler manuellement tous les ordres en attente
python -c "
import os
from broker.projectx_client import ProjectXClient
c = ProjectXClient(os.environ['PROJECTX_USERNAME'], os.environ['PROJECTX_API_KEY'])
c.login()
accounts = c.get_accounts()
acc = accounts[0]['id']
orders = c.get_open_orders(acc)
for o in orders:
    print(f'cancel {o[\"id\"]} → {c.cancel_order(acc, o[\"id\"])}')
"

# 5. Vérifier l'état broker via reconcile
python scripts/reconcile_daily.py
```

**Récupération**
- Identifier la cause dans `logs/trading_events.log` (signal qui se réarme, idempotence cassée).
- Pas de redémarrage automatique — investiguer d'abord.
- Si récidive après redémarrage, revenir au tag git précédent (`git tag -l | tail -5`).

---

## 2. WebSocket déconnecté > 5 min

**Symptômes**
- Logs : suite de `[INFO] Realtime WS démarrage` sans `[FILL]` ni `[CLÔTURE]`.
- Telegram envoie une alerte WS outage (si configurée).
- Daemon tmux toujours actif mais inactif au niveau market data.

**Action**
```bash
# 1. Vérifier que le PC a internet
curl -s -o /dev/null -w "%{http_code}\n" https://api.topstepx.com
# Attendu : 401 ou 200

# 2. Vérifier que Tailscale n'a PAS pris la route par défaut (cf. §sécurité)
ip route | grep default
# Attendu : default via 192.168.X.1 dev <interface normale>
# JAMAIS : default via 100.X.X.X dev tailscale0

# 3. Voir les dernières lignes du daemon (attache tmux)
tmux attach -t topstep
# Ctrl+B puis D pour détacher sans tuer

# 4. Si la WS ne reconnecte vraiment pas → restart propre
tmux kill-session -t topstep
tmux new -d -s topstep "python live.py"
tail -f logs/trading_events.log
```

**Vérification**
- `[INFO] Realtime WS démarrage` suivi de `[INFO] Realtime WS connecté` (ou équivalent).
- Premier `[CTX]` ou `[PRICE]` reçu en moins de 60s.

**Récupération**
- Si reconnexion réussit, observer pendant 10 min pour valider la stabilité.
- Si problème persiste, vérifier les credentials (`echo $PROJECTX_API_KEY` non vide) et la durée du token.

---

## 3. État interne désynchronisé du broker

**Symptômes**
- `python scripts/reconcile_daily.py` → exit code 1 (mismatch détecté).
- Telegram : `❌ Reconcile YYYY-MM-DD — mismatch détecté`.
- Cas typiques : position broker non présente dans state, ou inverse.

**Action**
```bash
# 1. Inspecter le détail du mismatch
python scripts/reconcile_daily.py --json | python -m json.tool

# 2. Ne RIEN modifier dans state/live_state.json à la main pendant que le daemon tourne
#    (race condition sur la persistance).
#    Si modification nécessaire, ARRÊTER le daemon d'abord.
tmux kill-session -t topstep
```

**Vérification**
- Identifier la nature du mismatch :
  - **Position broker orpheline** : le broker a une position qu'on n'a pas tracké
    → vérifier manuellement dans l'interface broker. Peut être un fill non capté
    par la WS. Décider de la fermer manuellement ou de l'enregistrer en state.
  - **Ordre orphelin broker** : ordre actif côté broker mais state ne le voit
    plus → annuler côté broker (cf. scénario 1 commande Python).
  - **Ordre orphelin state** : ordre marqué `PLACED` en state mais broker l'a
    déjà fermé → patcher manuellement le status à `CANCELLED` dans
    `state/live_state.json` (daemon arrêté).
  - **P&L hors tolérance** : généralement un trade aller mal-attribué. Vérifier
    via `client.get_trades_since(...)`.

**Récupération**
- Une fois le state corrigé manuellement, relancer reconcile pour confirmer
  l'alignement avant de redémarrer le daemon.
- Backup du state avant modif : `cp state/live_state.json state/live_state.json.bak_$(date +%s)`.

---

## 4. Limite Topstep approchée (< $200)

**Symptômes**
- Telegram : warning daily_loss / trailing_dd / consistency 50%.
- `live_state.json` : `realized_day_pnl` proche de `-USER_DAILY_LOSS_MAX`,
  ou `cum_pnl` proche de `peak_pnl - TOPSTEP_TRAILING_DD`.

**Action — PAS D'ACTION AUTOMATIQUE.**
La logique de blocage de `core/risk_portfolio.py::can_open` empêche déjà
l'ouverture de nouveaux trades. Le daemon va laisser les positions ouvertes
courir jusqu'à TP/SL/expiration.

Si tu veux **stopper toute nouvelle prise de risque** sans attendre :
```bash
# Option A — Arrêter le daemon (positions ouvertes restent à la merci de TP/SL)
tmux kill-session -t topstep

# Option B — Flatten (cf. scénario 6) si on veut couper net
```

**Vérification**
```bash
# Status actuel
cat state/live_state.json | python -m json.tool | grep -E "cum_pnl|peak_pnl|realized_day_pnl|consec_loss"
```

**Récupération**
- Si la session du jour est compromise : attendre la fin de session, laisser
  le daemon clôturer proprement. Le `_maybe_roll_day` reset les compteurs
  au début du jour suivant.
- Ne **jamais** modifier `core/risk_portfolio.py` "pour aider" pendant une session live.

---

## 5. Arrêt propre du daemon en urgence

**Action**
```bash
# 1. Signal d'arrêt propre (Ctrl+C dans la session, ou SIGTERM)
tmux send-keys -t topstep C-c
sleep 10                  # laisse le runner persister son état

# 2. Si le daemon ne réagit pas, kill la session tmux
tmux kill-session -t topstep

# 3. Vérifier qu'aucun process Python tourne plus
ps aux | grep -E "live_runner|live\.py" | grep -v grep
```

**Vérification**
- `tmux ls` ne liste plus `topstep`.
- `state/live_state.json` a sa `date` à jour (le runner sérialise au stop).

**Récupération**
- Le daemon peut être redémarré quand on veut (cf. scénario 8).
- Les ordres limites encore ouverts côté broker continuent leur vie sans
  surveillance — annuler manuellement si tu veux un état totalement propre.

---

## 6. Flatten toutes positions immédiatement

**Symptômes / Quand l'utiliser**
- Bug grave détecté pendant la session.
- Macro event imprévu (annonce surprise).
- Demande utilisateur explicite "tout fermer maintenant".

**Action**
```bash
# 1. Arrêter le daemon d'abord pour éviter qu'il re-poste
tmux kill-session -t topstep

# 2. Lister puis fermer toutes les positions ouvertes côté broker
python -c "
import os
from broker.projectx_client import ProjectXClient
c = ProjectXClient(os.environ['PROJECTX_USERNAME'], os.environ['PROJECTX_API_KEY'])
c.login()
acc = c.get_accounts()[0]['id']

# Annule TOUS les ordres en attente
for o in c.get_open_orders(acc):
    c.cancel_order(acc, o['id'])
    print(f'cancel order {o[\"id\"]}')

# Ferme TOUTES les positions ouvertes au marché
for p in c.get_positions(acc):
    side_close = 'short' if p['type'] == 0 else 'long'
    print(f'closing position {p[\"id\"]} on {p[\"contractId\"]} size={p[\"size\"]} side_close={side_close}')
    c.place_market_order(
        account_id=acc,
        contract_id=p['contractId'],
        side=side_close,
        size=p['size'],
    )
"
```

**Vérification**
```bash
# Toutes les positions et ordres doivent être à 0
python -c "
import os
from broker.projectx_client import ProjectXClient
c = ProjectXClient(os.environ['PROJECTX_USERNAME'], os.environ['PROJECTX_API_KEY'])
c.login()
acc = c.get_accounts()[0]['id']
print('positions:', c.get_positions(acc))
print('open_orders:', c.get_open_orders(acc))
"
```

**Récupération**
- Investiguer la cause avant tout redémarrage.
- Mettre à jour `state/live_state.json` manuellement (marquer tous les tags
  ouverts comme `CANCELLED` ou `FILLED` + `close_pnl` selon l'action prise).
- Reconcile pour valider l'alignement final.

---

## 7. Isoler un ticker problématique

**Symptômes**
- Un ticker (ex: YM1) produit des fills répétés `NOT_FILLED` ou des erreurs API.
- Edge cassé sur un ticker pendant que les autres tournent bien.

**Action**
```bash
# Désactiver le ticker dans config.py — modification temporaire à committer
# si elle dure plus d'une session
```

Édite `config.py` :
- `YM1_ENABLED = False`        (désactive globalement YM1 dans le live)
- `FIB_V4_TICKERS.remove("NQ1")` (retire NQ1 du subset Fib)
- `OPR_V5_1_TICKERS` ne contient plus le ticker (selon stratégie)

Puis :
```bash
# Restart pour appliquer la nouvelle config
tmux kill-session -t topstep
tmux new -d -s topstep "python live.py"
```

**Vérification**
- Plus aucune ligne `[SIGNAL]` ou `[ORDRE]` sur le ticker isolé dans les logs.
- Reconcile : positions sur ce ticker = 0 côté state.

**Récupération**
- Investiguer pourquoi le ticker pose problème (data ? edge ? broker ?).
- Réactiver via le flag inversé dans `config.py` quand corrigé.

---

## 8. Redémarrer après crash

**Pré-requis** : le daemon est arrêté (cf. scénarios 1, 5, 6).

```bash
# 1. Vérifier qu'aucun process zombie ne traîne
ps aux | grep -E "live_runner|live\.py" | grep -v grep

# 2. Vérifier qu'on est sur la branche prod
git status
git log --oneline -3

# 3. Vérifier la santé du state local
python scripts/reconcile_daily.py
# Exit code 0 = aligné, redémarrer avec confiance. Sinon corriger d'abord.

# 4. Vérifier l'invariant anti-VPN Tailscale (cf. §sécurité)
ip route | grep default
# Doit JAMAIS être : default via 100.X.X.X dev tailscale0

# 5. Démarrer dans tmux
tmux new -d -s topstep "python live.py"

# 6. Surveiller 5 min minimum
tail -f logs/trading_events.log
# Attendu en début de session :
#   [INFO] Realtime WS démarrage
#   [SESSION] Démarrage YYYY-MM-DD ...
#   [CTX] / [PRICE] / [SIGNAL] selon le moment de la journée
```

**Vérification**
- `tmux ls` liste `topstep`.
- Au moins 1 `[CTX]` ou heartbeat dans les 60s.
- `cat state/live_state.json | python -m json.tool | head -10` montre la date du jour.

---

## ⚠️ Encart sécurité Tailscale

> **Invariant Topstep (cf. CLAUDE.md) — Jamais de VPN sur le trafic Topstep.**
> Tailscale est utilisé **uniquement** pour le dashboard iPhone (mesh privé,
> n'affecte pas la route internet par défaut). Si Tailscale prend la route
> par défaut (mode exit-node), le trafic Topstep transite via Tailscale, ce
> qui peut **violer les T&C de la prop firm** et provoquer une fermeture
> de compte.

### Commandes de vérification au démarrage du daemon

```bash
# 1. Route par défaut → ne doit PAS pointer vers tailscale0
ip route | grep default
# ATTENDU : default via 192.168.X.1 dev eth0 (ou interface normale)
# JAMAIS  : default via 100.X.X.X dev tailscale0

# 2. IP publique vue de l'extérieur = IP ISP
curl -s ifconfig.me
# Doit être ton IP ISP, identique à avant Tailscale.

# 3. Traceroute vers Topstep — premier hop = gateway local
traceroute api.topstepx.com 2>/dev/null | head -3
# Premier hop attendu : 192.168.X.1 (ta box), JAMAIS 100.X.X.X
```

### Si l'un des checks échoue

```bash
# STOP — ne pas démarrer le live
# Désactive l'exit-node si actif
sudo tailscale up --reset
# Puis ré-essayer les 3 checks. Si toujours faux, désinstaller Tailscale :
# sudo tailscale down ; sudo apt remove tailscale (Debian) ou équivalent

# Un check automatique au démarrage du daemon refusera de lancer si
# l'IP publique tombe dans la plage Tailscale 100.64.0.0/10
# (cf. broker/live_runner._check_no_vpn_on_topstep — PHASE 1.4.g).
```

### Quand Tailscale est OK

- Le PC est joignable depuis l'iPhone via `http://topstep-pc:8501` ou
  `http://100.X.X.X:8501` (port du dashboard Streamlit).
- Le trafic vers `api.topstepx.com` passe par l'interface normale (eth0/wlan0).
- `ip route | grep default` montre `default via <gateway local>`, pas
  `dev tailscale0`.

---

## Logs utiles

| Source | Commande | Quand l'utiliser |
|---|---|---|
| Trading events | `tail -f logs/trading_events.log` | Live : voir signaux, fills, closes |
| Risk events | `grep RISK logs/trading_events.log` | Investiguer un blocage RM |
| WS errors | `grep -E "WS\|websocket" logs/trading_events.log` | Investiguer une déconnexion |
| Telegram | App Telegram | Alertes proactives, /status sur demande |

---

## Backups

```bash
# Avant tout changement risqué
cp state/live_state.json state/live_state.json.bak_$(date +%Y%m%d_%H%M%S)
tar czf logs_backup_$(date +%Y%m%d).tar.gz logs/

# Tag git défensif
git tag -a v$(date +%Y%m%d-%H%M)-pre-intervention -m "État avant intervention"
```

---

*Runbook d'incident — réponse opérationnelle live.*
