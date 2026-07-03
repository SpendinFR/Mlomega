# MLOmega V18.8.1 — démarrage ici

Utilise uniquement les scripts V18.8 à la racine :

```powershell
.\INSTALL_MLOMEGA_V18_8_WINDOWS.ps1 -HfToken "hf_..." -PersonId "me"
.\RUN_MLOMEGA_V18_8.ps1 -PersonId me
.\STOP_MLOMEGA_V18_8.ps1 -PersonId me
.\RESUME_MLOMEGA_V18_8.ps1 -PersonId me
.\DOCTOR_MLOMEGA_V18_8.ps1 -Full -Bridge -Delivery
```

Le profil `CORE_BRAINLIVE_V18_8_PHONE` utilise SQLite, Qdrant, Ollama, BrainLive, deep audio, deep vision, Brain2 et le Phone Bridge. Il n’utilise pas Neo4j, Graphiti ni Mem0.

Lis `GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md` avant la première installation.

Guide complet : `GUIDE_INSTALL_MLOMEGA_V18_8_RUNTIME.md` ; versions PDF/Word : `GUIDE_MLOMEGA_V18_8_INSTALL_RUN_RESUME.pdf` et `.docx`.


V18.8.1 ajoute la chaîne de preuve image complète jusqu’au deep vision/Brain2 et bloque le post-stop si un bundle contient des frames mais aucune image brute lisible.


V18.8.1 ajoute la chaîne de preuve image complète jusqu’au deep vision/Brain2 et bloque le post-stop si un bundle contient des frames mais aucune image brute lisible.
