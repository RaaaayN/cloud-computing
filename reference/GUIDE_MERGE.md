# Guide de merge — Elastic ML Inference Serving

Projet `RaaaayN/cloud-computing`, équipe de 4. Objectif : réunir les 4 branches dans un dépôt qui tourne de bout en bout, puis lancer l'expérience finale (autoscaler custom vs HPA 70 % et 90 %) avant la deadline du 25 juin.

---

## 0. État des lieux

Il y a 4 branches. Voici la correspondance rôle → branche :

| Rôle | Branche | Contenu réel |
|------|---------|--------------|
| Base fournie par le cours | `main` | `model_server.py` (inférence aiohttp), `client.py`, `zidane.jpg`, PDF, notes |
| The Brain (Autoscaler) | `elastic-autoscaler` | **Monorepo complet déjà restructuré** : `src/autoscaler`, `src/dispatcher`, `src/load_tester`, `k8s/`, `tests/`, `docs/`, README |
| The DevOps + ML Dev | `infra-setup` | `inference-service/` (Flask + Docker), `k8s-manifest/` |
| The Traffic Manager | `load-tester` | `dispatcher/dispatcher_redis.py` (Flask + Redis), `workload.txt` |

**Le constat le plus important :** la branche `elastic-autoscaler` n'est pas « juste l'autoscaler ». C'est déjà une intégration propre de tout le projet (inférence + dispatcher + load tester + autoscaler + Prometheus + tests + docs), avec un contrat d'API unique et un README. Les branches `infra-setup` et `load-tester` contiennent des **versions concurrentes et incompatibles** des mêmes composants.

Autrement dit : il ne faut **pas** faire un `git merge` des trois branches à plat. Cela créerait deux dispatchers, deux services d'inférence et deux jeux de manifestes K8s en conflit. La bonne stratégie est de **partir de `elastic-autoscaler` comme base** et de n'y réintégrer que les rares morceaux utiles des autres.

### Les deux « stacks » incompatibles

Deux chaînes cohérentes mais incompatibles ont émergé. Il faut en garder **une seule**.

| | Stack A — `elastic-autoscaler` (à garder) | Stack B — `infra-setup` + `load-tester` (à abandonner) |
|--|--|--|
| Contrat image | base64 `{"data": "<b64>"}` | chemin fichier `{"image": "<path>"}` |
| Inférence | `model_server.py` aiohttp, `/infer`, port 8001 | `inference-service/app.py` Flask, `/`, port 6001 |
| Dispatcher | `src/dispatcher/app.py` aiohttp, file `asyncio.Queue`, `/submit`, port 8002 | `dispatcher_redis.py` Flask+Redis, `/query`, port 5001 |
| Métrique file | `dispatcher_queue_depth` | `dispatcher_queue_size` |
| Autoscaler | `src/autoscaler/` (paquet MAPE propre) | `autoscaler.py` / `autoscaler_logger.py` (scripts, buggés) |

Le contrat « chemin de fichier » de la Stack B est de toute façon **inutilisable en conditions réelles** : un chemin local ne traverse pas le réseau entre conteneurs/replicas. Le contrat base64 est le bon.

---

## 1. Décisions à acter en équipe (avant toute commande git)

1. **Branche d'intégration = `elastic-autoscaler`.** On y ramène le strict nécessaire, puis on la fusionne dans `main` à la fin.
2. **Contrat unique : base64 `{"data": ...}`**, endpoints `/infer` (inférence) et `/submit` (dispatcher). Tout ce qui utilise `{"image": path}` est abandonné.
3. **On garde** : `src/autoscaler`, `src/dispatcher`, `src/load_tester`, `model_server.py`, `k8s/`, `tests/`, `docs/`.
4. **On abandonne** : `inference-service/app.py`, `inference-service/model.py`, `dispatcher_redis.py`, `k8s-manifest/`, les `__pycache__/*.pyc`, les deux gros `cat.jpg` (8,8 Mo chacun).
5. **On récupère seulement** : `workload.txt` (la vraie trace de charge) depuis `load-tester`, et l'**idée** d'instrumentation Prometheus de `infra-setup` (à reporter sur `model_server.py`).

> Présentez ce tableau aux 4 et validez-le explicitement. C'est la seule étape « politique » ; tout le reste est mécanique.

---

## 2. Préparer la branche d'intégration

```bash
git fetch --all
git switch elastic-autoscaler
git pull
git switch -c integration         # branche de travail, on ne touche pas main tout de suite
```

Vérifiez l'arbre de départ :

```bash
git ls-files | head -50
python -m pytest tests/ -v        # doit passer AVANT d'ajouter quoi que ce soit
```

Si les tests ne passent pas déjà sur `elastic-autoscaler`, réglez ça d'abord — c'est votre socle de référence.

---

## 3. Récupérer les rares morceaux utiles des autres branches

### 3.1 La trace de charge réelle (depuis `load-tester`)

```bash
git checkout load-tester -- dispatcher/test/workload.txt
git mv dispatcher/test/workload.txt src/load_tester/workload.txt   # si git mv échoue, mkdir puis mv manuel
```

Ouvrez `src/load_tester/run.py` et vérifiez s'il sait lire un fichier de trace. S'il génère seulement un profil « triangle », ajoutez une option `--workload src/load_tester/workload.txt` pour rejouer la vraie trace fournie par le cours (c'est ce qui sera attendu à la soutenance).

### 3.2 Le Dockerfile d'inférence (inspiré de `infra-setup`)

`elastic-autoscaler` contient `docker/Dockerfile.loadtester` mais **pas** de Dockerfile pour l'inférence, alors que `k8s/inference-deployment.yaml` référence l'image `inference:latest`. Il faut le créer (voir §5.1). Servez-vous de `infra-setup/inference-service/Dockerfile` comme modèle, mais en pointant sur `model_server.py`, pas sur le Flask.

### 3.3 Nettoyage

```bash
# s'assurer que ces poids lourds / artefacts ne reviennent jamais
printf '\n__pycache__/\n*.pyc\n*.jpg\n!zidane.jpg\n' >> .gitignore
git rm -r --cached --ignore-unmatch '**/__pycache__' '**/*.pyc'
```

Ne committez jamais les `cat.jpg` de 8,8 Mo ni les `.pyc` présents dans `infra-setup`.

---

## 4. LE correctif critique — instrumenter `model_server.py`

C'est le point de blocage n°1. À l'état actuel, `model_server.py` (la version aiohttp de `main`/`elastic-autoscaler`) **n'expose aucune métrique Prometheus et aucun endpoint `/metrics`, `/healthz`, `/readyz`**. Or :

- `k8s/inference-deployment.yaml` définit une `readinessProbe` sur `/readyz` et une `livenessProbe` sur `/healthz`. Sans ces routes, **les pods d'inférence ne passeront jamais `Ready`** et le déploiement échouera en boucle.
- `src/autoscaler/controller.py` calcule la p99 avec la requête `histogram_quantile(0.99, sum(rate(inference_duration_seconds_bucket[1m])) by (le))`. Sans la métrique `inference_duration_seconds`, **la p99 vaut toujours 0** (le client Prometheus renvoie 0 quand la série est vide) et l'autoscaler ne réagira jamais à la latence — alors que la latence < 0,5 s est précisément le SLO du projet.
- Le `configmap.yaml` de Prometheus scrape déjà `inference:8001/metrics` → aujourd'hui c'est un 404.

La doc (`docs/ARCHITECTURE.md`) *prétend* que ces métriques existent (`inference_requests_total`, `inference_duration_seconds`) : c'est faux dans le code. À corriger.

`infra-setup/inference-service/app.py` contient bien un `Histogram` Prometheus — mais sous le nom `inference_latency_seconds`, sur du Flask filepath. **N'importez pas ce fichier.** Reportez seulement l'instrumentation sur `model_server.py`, avec le **nom exact attendu par le controller : `inference_duration_seconds`**.

Patch à appliquer à `model_server.py` :

```python
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

INFERENCE_REQUESTS_TOTAL = Counter(
    "inference_requests_total", "Total inference requests"
)
# buckets resserrés autour du SLO 0,5 s pour une p99 exploitable
INFERENCE_DURATION = Histogram(
    "inference_duration_seconds",
    "Server-side inference duration",
    buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0, 5.0),
)

def infer(d):
    INFERENCE_REQUESTS_TOTAL.inc()
    with INFERENCE_DURATION.time():
        # ... corps existant de infer() ...
        return labels

async def metrics_handler(_):
    return web.Response(body=generate_latest(), content_type=CONTENT_TYPE_LATEST)

async def healthz_handler(_):
    return web.json_response({"status": "ok"})

async def readyz_handler(_):
    return web.json_response({"status": "ready"})

app.add_routes([
    web.post("/infer", infer_handler),
    web.get("/metrics", metrics_handler),
    web.get("/healthz", healthz_handler),
    web.get("/readyz", readyz_handler),
])
```

Ajoutez `prometheus_client` à `requirements.txt`. Test immédiat :

```bash
python model_server.py &
curl -s localhost:8001/healthz
curl -s localhost:8001/metrics | grep inference_duration_seconds_bucket
```

Tant que cette commande ne renvoie pas de lignes `inference_duration_seconds_bucket{le=...}`, **rien d'autre ne sert** : l'autoscaler restera aveugle à la latence.

---

## 5. Créer les livrables encore manquants

Le README liste « Planned » trois choses indispensables à la note. À faire :

### 5.1 `docker/Dockerfile.inference`

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.3.0 torchvision==0.18.0 \
      --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt prometheus_client
COPY model_server.py .
EXPOSE 8001
CMD ["python", "model_server.py"]
```

### 5.2 Manifestes HPA (baseline obligatoire pour la comparaison)

Le projet exige de lancer l'expérience **une fois avec l'autoscaler custom et deux fois avec le HPA (cibles 70 % et 90 % CPU)**. Ces manifestes n'existent pas → créez `k8s/hpa-70.yaml` et `k8s/hpa-90.yaml` :

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: inference-hpa
  namespace: inference-system
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: inference
  minReplicas: 1
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70   # 90 dans hpa-90.yaml
```

### 5.3 Harness de comparaison + figure

Les fichiers que tu as en local (`autoscaler.py`, `autoscaler_logger.py`, `analyze_autoscaler_log.py`) sont une **ancienne** version de l'autoscaler et sont buggés : seuil `p99_latency > 0.005` (au lieu de 0.5), colonne `Queue_Size` lue puis jamais définie, métrique `dispatcher_queue_size` qui n'existe pas dans le dispatcher retenu (`dispatcher_queue_depth`). **Ne les remettez pas dans le repo.** Le vrai autoscaler est `src/autoscaler/`.

À la place, écrivez un petit script `experiments/collect.py` qui, pendant chaque run, interroge Prometheus toutes les 15 s et enregistre en CSV : `timestamp, p99_latency, replica_count, cpu_cores`. Les requêtes PromQL à utiliser (toutes déjà cohérentes avec le stack A) :

```promql
# p99 latence serveur
histogram_quantile(0.99, sum(rate(inference_duration_seconds_bucket[1m])) by (le))
# nombre de replicas
kube_deployment_status_replicas{deployment="inference"}   # ou: count(up{job="inference"})
# cœurs CPU consommés
sum(rate(container_cpu_usage_seconds_total{pod=~"inference.*"}[1m]))
```

Puis un script `experiments/plot.py` qui superpose les 3 runs (custom / HPA70 / HPA90) sur deux figures : p99 vs temps, et nombre de cœurs CPU vs temps. C'est exactement la « figure time-series » demandée à la slide 17.

---

## 6. Tests — un par un, couche par couche

Ne testez jamais tout d'un coup. Validez chaque couche avant de monter la suivante.

### 6.1 Tests unitaires (aucune infra requise)

```bash
python -m pytest tests/ -v
```

Doivent passer : `test_scaling_logic`, `test_prometheus_queries`, `test_k8s_patch`, `test_dispatcher_forward`, `test_load_tester`. **Critère :** tout vert.

### 6.2 Bout-en-bout local, 3 process (pas encore K8s)

Terminal 1 — inférence :
```bash
python model_server.py
```
Terminal 2 — dispatcher (pointé sur l'inférence locale) :
```bash
export INFERENCE_URL=http://127.0.0.1:8001        # Windows: set INFERENCE_URL=...
python src/dispatcher/app.py
```
Terminal 3 — un appel unique puis la charge :
```bash
python client.py                                   # smoke test 1 requête
python src/load_tester/run.py --target http://127.0.0.1:8002 --duration 60 --base 2 --peak 10
```
**Critères de validation :**
```bash
curl -s localhost:8001/metrics | grep inference_duration_seconds_bucket   # non vide
curl -s localhost:8002/metrics | grep dispatcher_queue_depth              # non vide
curl -s localhost:8003/metrics | grep loadtester_request                  # non vide pendant le run
```
Si une de ces trois lignes est vide, corrigez avant d'aller plus loin.

### 6.3 Construire les images et les charger dans Minikube

```bash
minikube start --cpus=4 --memory=6g
minikube addons enable metrics-server          # indispensable pour le HPA
eval $(minikube docker-env)                    # ou: docker build puis minikube image load
docker build -t inference:latest      -f docker/Dockerfile.inference .
docker build -t loadtester:latest     -f docker/Dockerfile.loadtester .
# (le dispatcher a-t-il un Dockerfile ? sinon en créer un sur le même modèle)
minikube image load inference:latest
minikube image load loadtester:latest
```
`imagePullPolicy: IfNotPresent` + image locale → le `minikube image load` est obligatoire, sinon `ErrImagePull`.

### 6.4 Déploiement K8s, composant par composant

Appliquez dans l'ordre et **attendez `Ready` après chaque étape** :

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/inference-deployment.yaml
kubectl -n inference-system rollout status deploy/inference     # DOIT devenir Ready (cf. §4)
kubectl apply -f k8s/dispatcher-deployment.yaml
kubectl -n inference-system rollout status deploy/dispatcher
kubectl apply -f k8s/prometheus/
kubectl -n inference-system rollout status deploy/prometheus
```
**Critère :** `kubectl -n inference-system get pods` → tout `Running` + `READY 1/1`. Si l'inférence reste `0/1`, c'est le correctif §4 qui manque (`/readyz`).

### 6.5 Vérifier que Prometheus voit bien les cibles

```bash
kubectl -n inference-system port-forward svc/prometheus 9090:9090
# navigateur: http://localhost:9090/targets  → inference, dispatcher, loadtester en UP (vert)
```
Dans l'onglet *Graph*, exécutez la requête p99 ci-dessus : elle doit renvoyer une valeur numérique, pas « no data ». **C'est le test qui prouve que la chaîne métrique est branchée.**

### 6.6 Autoscaler en dry-run d'abord

```bash
kubectl apply -f k8s/autoscaler-deployment.yaml      # dry-run par défaut dans le manifeste
kubectl -n inference-system logs -f deploy/autoscaler
```
Vous devez voir des lignes `MAPE decision reason=... queue_depth=... p99=... current=... desired=...` avec des valeurs **non nulles** sous charge. Tant que c'est en dry-run, il décide sans patcher — parfait pour vérifier la logique sans risque.

> ⚠️ Avant le run réel : dans `controller.py`, le namespace par défaut est `default` alors que tout est déployé dans `inference-system`. Passez `DEPLOYMENT_NAMESPACE=inference-system` (env du manifeste autoscaler), sinon le patch de replicas visera le mauvais namespace. Vérifiez aussi que le RBAC autorise bien le `patch` du Deployment.

---

## 7. Expérience finale, en conditions réelles

Objectif (slide 17) : **3 runs** sur la même trace de charge, mêmes paramètres, et comparer p99 et nombre de cœurs CPU.

Règles à respecter pour que la comparaison soit valable :
- **Un seul scaler à la fois.** Ne jamais laisser tourner le HPA et l'autoscaler custom en même temps sur le même Deployment.
- Même `workload.txt`, même durée, même cluster, mêmes ressources pod (CPU request/limit = 1, déjà en place).
- Repartir d'un état propre entre chaque run (replicas remis à `minReplicas`, attendre le retour au calme).

**Run 1 — autoscaler custom :**
```bash
kubectl -n inference-system delete hpa --all                 # pas de HPA
# désactiver le dry-run: --dry-run retiré des args du manifeste autoscaler
kubectl apply -f k8s/autoscaler-deployment.yaml
python experiments/collect.py --out custom.csv &
kubectl apply -f k8s/loadtester-job.yaml                     # rejoue la trace
# à la fin du job: arrêter collect.py
```

**Run 2 — HPA 70 % :**
```bash
kubectl -n inference-system delete deploy autoscaler         # couper le custom
kubectl scale -n inference-system deploy/inference --replicas=1
kubectl apply -f k8s/hpa-70.yaml
python experiments/collect.py --out hpa70.csv &
kubectl apply -f k8s/loadtester-job.yaml
```

**Run 3 — HPA 90 % :** identique avec `kubectl delete hpa inference-hpa` puis `k8s/hpa-90.yaml` et `--out hpa90.csv`.

**Production de la figure :**
```bash
python experiments/plot.py custom.csv hpa70.csv hpa90.csv
```
Vous obtenez les deux graphiques attendus (p99 vs temps, cœurs CPU vs temps, les 3 courbes superposées). L'argument à défendre : le custom voit la congestion **en avance** via `queue_depth` là où le HPA réagit après la montée CPU → p99 plus stable sous le SLO 0,5 s, à coût CPU comparable ou inférieur.

---

## 8. Pièges à surveiller (récapitulatif)

1. **`inference_duration_seconds` manquant** → §4. Bloque la readiness des pods ET la p99 de l'autoscaler. Priorité absolue.
2. **`dispatcher_queue_depth` (stack A) ≠ `dispatcher_queue_size` (ancien)**. Ne réintroduisez pas les vieux scripts `autoscaler*.py` qui interrogent le mauvais nom.
3. **Namespace** : `controller.py` a `default` par défaut, tout le reste est en `inference-system`. Forcez `DEPLOYMENT_NAMESPACE=inference-system`.
4. **Autoscaler en dry-run par défaut** : à désactiver pour les runs réels, et vérifier le RBAC (`patch deployments`).
5. **Images locales + `IfNotPresent`** : toujours `minikube image load ...`.
6. **Contrat filepath `{"image": path}`** (stack B) : abandonné, inutilisable en réseau.
7. **`model.half()` sur CPU** (infra-setup) : à ne pas reprendre, FP16 sur CPU est lent/instable. La version `model_server.py` (PIL + `weights.transforms()`) est la bonne.
8. **Gros binaires / `.pyc`** : `cat.jpg` 8,8 Mo et `__pycache__` ne doivent pas entrer dans le repo.
9. **HPA** : nécessite `metrics-server` activé et les CPU requests (déjà présents). Sans metrics-server, le HPA reste `unknown`.
10. **Fusion finale** : une fois `integration` validée de bout en bout, `git switch main && git merge integration`, puis taggez la version de rendu.

---

## Ordre d'attaque conseillé (résumé)

1. Valider en équipe : base = `elastic-autoscaler`, contrat base64 (§1).
2. Brancher `integration`, faire passer les tests existants (§2).
3. Récupérer `workload.txt`, nettoyer (§3).
4. **Corriger `model_server.py`** (§4) — sans ça rien ne marche.
5. Créer Dockerfile inférence, HPA 70/90, harness de mesure (§5).
6. Tester couche par couche : unitaires → local 3 process → Minikube → cibles Prometheus UP → autoscaler dry-run (§6).
7. 3 runs réels + figure de comparaison (§7).
8. Merge dans `main` et tag.