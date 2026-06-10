# brouillon/ — dossier de travail jetable

**Tout travail d'essai vit ici.** La base du projet (live + outillage + docs) reste propre :
aucun essai ne doit être créé en dehors de ce dossier.

## Convention

| Tu veux… | Mets-le dans… |
|---|---|
| Une stratégie d'essai | `brouillon/strategies/<nom>.py` → découverte par `backtest.py --strategy <nom>` et `optimize.py` |
| Un script d'analyse / diagnostic | `brouillon/scripts/<nom>.py` → `python brouillon/scripts/<nom>.py` |
| Des notes, hypothèses, résultats | `brouillon/notes/<nom>.md` |

Les résultats de backtest vont dans `output/` (à la racine, gitignoré) comme d'habitude.

## Vider le brouillon

Quand tu me demandes de **« vider le brouillon »**, je lance :

```bash
bash scripts/clear_brouillon.sh          # vide brouillon/ + output/ + mémoire
bash scripts/clear_brouillon.sh --dry-run # prévisualise sans rien supprimer
```

Cela supprime tout le contenu d'essai et **ramène le projet à l'état de base**.
Le scaffold (`strategies/__init__.py`, `scripts/`, `notes/`, ce README) est conservé.

## Git

`brouillon/` est **gitignoré** (sauf ce scaffold). Les essais ne polluent jamais l'historique
git : la base committée = le projet **sans** brouillon. Aucune opération git n'est nécessaire
pour revenir à l'état initial — il suffit de vider le dossier.
