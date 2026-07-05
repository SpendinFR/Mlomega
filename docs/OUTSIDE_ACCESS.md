# OUTSIDE_ACCESS — utiliser MLOmega V19 dehors (E36)

L'usage principal de MLOmega V19 est **dehors** : le téléphone (lunettes XREAL /
S25) est en 4G/5G, le PC reste **à la maison, derrière la box (NAT)**. Une IP LAN
seule (`192.168.x.x`) ne marche que sur ton réseau domestique. Ce guide met en
place un **tunnel VPN** privé (Tailscale, recommandé) pour que le téléphone
atteigne le PC exactement comme sur le LAN — sans ouvrir de port sur Internet.

> **Rappel honnête.** Dehors **sans tunnel**, le PC est injoignable : le contexte
> live (VisionRT, WorldBrain, BrainLive, mémoire) ne tourne pas. Il te reste le
> mode **Ultra-Live** (réflexes sur le téléphone : sous-titres, wake word, gestes)
> — c'est un mode dégradé assumé, jamais une fausse promesse. Avec le tunnel, tout
> le live contextuel repasse.

---

## 1. Pourquoi un tunnel (et pas un port forwarding)

| Option | Verdict |
|---|---|
| **Tailscale** (WireGuard managé) | **Recommandé.** Chiffré, zéro port ouvert sur la box, IP stable `100.x.y.z`, marche derrière n'importe quel NAT/CGNAT. |
| **WireGuard manuel** | OK si tu sais faire. Plus de travail (clés, endpoint public, port UDP à ouvrir). Voir §6. |
| **Port forwarding** (ouvrir 8710 sur la box) | **Déconseillé.** Expose le PC sur Internet ; dépend d'une IP publique fixe ; CGNAT casse tout. À éviter. |

Le serveur écoute déjà sur **toutes les interfaces** (`0.0.0.0`) et **le token de
session** est la barrière d'accès (un pair sans token reçoit HTTP 401). Le tunnel
ajoute le chiffrement réseau ; le token ajoute l'authentification applicative.

---

## 2. Installer Tailscale — PC Windows

1. Télécharge Tailscale : <https://tailscale.com/download/windows>.
2. Installe, lance, **connecte-toi** (compte Google/Microsoft/GitHub — un compte
   perso suffit, plan gratuit).
3. Une fois connecté, récupère l'**IP 100.x** du PC :
   - clic droit sur l'icône Tailscale (barre des tâches) → l'IP `100.a.b.c` est
     affichée ; ou
   - PowerShell : `tailscale ip -4` → renvoie `100.a.b.c`.
4. Laisse Tailscale actif (démarrage automatique conseillé). C'est **cette IP
   100.x** que le téléphone utilisera dehors.

## 3. Installer Tailscale — téléphone Android (XREAL / S25)

1. Play Store → **Tailscale** → installer.
2. Ouvre, **connecte-toi avec le MÊME compte** que le PC (c'est ce qui relie les
   deux appareils dans ton réseau privé « tailnet »).
3. Active le VPN (bascule « Connected »). Le téléphone voit maintenant le PC via
   son IP `100.x`, en 4G/5G comme en Wi-Fi.

> Astuce : sur le tailnet, tu peux tester depuis le navigateur du téléphone :
> `http://100.a.b.c:8710/health` doit renvoyer `{"status":"ok",...}`.

---

## 4. Remplir le profil / la config

### 4.1 Profil PC — `configs/user_profile.yaml`

Ajoute la **liste ordonnée** d'endpoints (LAN d'abord, tunnel ensuite) :

```yaml
endpoints:
  - {name: lan,       host: 192.168.1.10,    port: 8710}   # à la maison
  - {name: tailscale, host: 100.101.102.103, port: 8710}   # dehors (ton IP 100.x)
```

Le client essaie **dans l'ordre** : à la maison le LAN répond (latence minimale) ;
dehors le LAN échoue, il **bascule** sur Tailscale. Au retour maison, la
reconnexion **re-teste le LAN d'abord** → retour automatique au LAN.

Le serveur bind déjà `0.0.0.0`. Pour restreindre l'écoute à la seule interface
Tailscale : `bind_host: 100.101.102.103` (optionnel).

### 4.2 Unity — `MLOmegaConfig`

Dans l'asset `MLOmegaConfig` (Inspector), section **« Outside access — endpoint
failover »** : renseigne la liste `Endpoints` (name/host/port), LAN en premier,
Tailscale ensuite. Vide → l'ancien `PcHost` unique est utilisé (rétrocompatible).
`SessionPairing` sonde `/health` dans l'ordre et bascule tout seul.

### 4.3 Android transport (Kotlin)

`SignalingClient` accepte la même liste ordonnée d'endpoints : `/health` sondé
dans l'ordre, `POST /webrtc/offer` sur le premier joignable, failover sinon.

### 4.4 Companion-web (téléphone/PC, viewer)

Ouvre le viewer avec la liste en query :
`http://<viewer>/?endpoints=192.168.1.10,100.101.102.103` (le premier `/health`
qui répond fixe l'URL WebSocket). `?ws=` force une URL précise si besoin.

### 4.5 Simulateur PC (`fake_xr_device.py`)

```
python simulators/fake_xr_device.py --endpoints 192.168.1.10:8710,100.101.102.103:8710 --token <TOKEN> --frames 30
```

Il résout le premier endpoint joignable et négocie la WebRTC dessus ; si aucun ne
répond il imprime `pc_unreachable` (mode réflexe device only).

---

## 5. WebRTC à travers le tunnel (pourquoi ça passe sans TURN)

En VPN type Tailscale, l'adresse `100.x` du PC est une **adresse routable pour le
téléphone** : aiortc/GetStream la présente comme un **host candidate** ICE
ordinaire. La connexion média se fait donc **directement à travers le tunnel**,
**sans serveur TURN ni relais externe** (politique local-first : aucun relais tiers
par défaut). Il suffit que :

- le SessionHub écoute sur `0.0.0.0` (défaut) — le pair Tailscale l'atteint comme
  un pair LAN ;
- le **token de session** garde l'accès (déjà en place) ;
- Tailscale soit actif des deux côtés.

Aucune config ICE supplémentaire n'est requise pour le cas Tailscale.

---

## 6. Alternative — WireGuard manuel (avancé)

1. Installe WireGuard sur le PC et le téléphone.
2. Génère une paire de clés par pair ; échange les clés publiques.
3. Le PC est le pair avec un `Endpoint` joignable (nécessite une IP publique/port
   UDP ouvert — c'est la partie pénible, d'où la recommandation Tailscale).
4. Assigne des IP privées (ex. `10.9.0.1` PC, `10.9.0.2` téléphone) et mets
   l'IP privée du PC dans `endpoints:` à la place de l'IP `100.x`.

Le port forwarding brut (ouvrir 8710 sur la box) reste **déconseillé** (exposition
Internet + CGNAT).

---

## 7. Dégradation réseau (WAN)

Dehors, la latence 4G/5G est plus haute (≈ 40–120 ms). MLOmega applique un profil
réseau **WAN** distinct du LAN (`degraded.py`) :

- seuil de latence relevé (pas de « network_degraded » qui clignote sans arrêt) ;
- **résolution vidéo cible abaissée** (720p LAN → 540p WAN) pour ne pas saturer le
  tunnel ;
- **cadences détecteur côté PC inchangées** (elles tournent en local, pas sur le
  lien) ;
- les **chemins réflexes du téléphone ne dépendent pas du PC** — ils tournent quoi
  qu'il arrive.

Le lien actif (`lan`/`wan`) et l'endpoint résolu apparaissent sur `/metrics`
(`active_link`, `active_endpoint`, `target_video_height`).

---

## 8. Checklist de validation (à faire par l'utilisateur, sur 4G réelle)

Coupe le Wi-Fi du téléphone (**vraie 4G/5G**), Tailscale actif des deux côtés :

- [ ] **Health depuis la 4G** : navigateur téléphone → `http://100.a.b.c:8710/health`
      renvoie `{"status":"ok"}`.
- [ ] **Session** : lance le client (lunettes/S25 ou `fake_xr_device --endpoints …
      --token …`) → une session se crée, `active_endpoint = tailscale` sur
      `/metrics`.
- [ ] **Live contextuel** : une carte/contour apparaît (VisionRT/WorldBrain
      tournent côté PC via le tunnel).
- [ ] **Latence attendue** : sous-titres/cartes arrivent avec un délai raisonnable
      (typ. +40–120 ms vs LAN) ; `active_link = wan`, `target_video_height = 540`.
- [ ] **Retour maison** : rebranche le Wi-Fi domestique → à la reconnexion,
      `active_endpoint` repasse à `lan` automatiquement.
- [ ] **Sans tunnel** : coupe Tailscale → le PC est injoignable, le téléphone
      bascule en **Ultra-Live** (réflexes locaux) — comportement dégradé attendu,
      pas un plantage.
