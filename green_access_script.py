import arcpy
import os

# =========================
# RÉCUPÉRATION DES PARAMÈTRES
# =========================

# Paramètre 0 : couche des communes (polygone, EPSG:4326)
communes = arcpy.GetParameterAsText(0)

# Paramètre 1 : couche des espaces verts (polygone, EPSG:4326)
espaces_verts = arcpy.GetParameterAsText(1)

# Paramètre 2 : couche des routes (ligne, EPSG:4326)
routes = arcpy.GetParameterAsText(2)

# Paramètre 3 : distance du buffer en mètres (ex. : 300)
distance_buffer = float(arcpy.GetParameterAsText(3))

# Paramètre 4 : couche de sortie (communes enrichies)
output_fc = arcpy.GetParameterAsText(4)

# Paramètre 5 : workspace (GDB de travail pour les fichiers intermédiaires)
workspace = arcpy.GetParameterAsText(5)

# Configuration de l'environnement de géotraitement
arcpy.env.workspace = workspace
arcpy.env.overwriteOutput = True

# =========================
# VALIDATIONS AVANT TRAITEMENT
# =========================

# 1. Vérifier que la couche des communes existe
if not arcpy.Exists(communes):
    arcpy.AddError("La couche 'communes' n'existe pas ou le chemin est incorrect.")
    raise SystemExit

# 2. Vérifier que la couche des espaces verts existe
if not arcpy.Exists(espaces_verts):
    arcpy.AddError("La couche 'espaces_verts' n'existe pas ou le chemin est incorrect.")
    raise SystemExit

# 3. Vérifier que la couche des routes existe
if not arcpy.Exists(routes):
    arcpy.AddError("La couche 'routes' n'existe pas ou le chemin est incorrect.")
    raise SystemExit

# 4. Vérifier que la distance est strictement positive
if distance_buffer <= 0:
    arcpy.AddError("La distance du buffer doit être strictement supérieure à 0.")
    raise SystemExit

# 5. Vérifier que la couche de sortie n'existe pas déjà (évite l'écrasement involontaire)
if arcpy.Exists(output_fc):
    arcpy.AddError("La couche de sortie existe déjà. Veuillez choisir un autre nom ou supprimer la couche existante.")
    raise SystemExit

# 6. Vérifier que le workspace est accessible
if not arcpy.Exists(workspace):
    arcpy.AddError("Le workspace spécifié n'existe pas ou n'est pas accessible.")
    raise SystemExit

# =========================
# EXÉCUTION DU TRAITEMENT
# =========================

try:
    # --- Chemins des fichiers intermédiaires ---
    # Couches reprojetées (EPSG:4326 -> Lambert 93)
    tmp_communes_proj   = os.path.join(workspace, "_tmp_communes_proj")
    tmp_ev_proj         = os.path.join(workspace, "_tmp_ev_proj")
    tmp_routes_proj     = os.path.join(workspace, "_tmp_routes_proj")
    # Couches de géotraitement
    tmp_buffer_ev       = os.path.join(workspace, "_tmp_buffer_ev")
    tmp_routes_clip     = os.path.join(workspace, "_tmp_routes_clip")
    tmp_routes_communes = os.path.join(workspace, "_tmp_routes_communes")
    tmp_stats           = os.path.join(workspace, "_tmp_stats")

    # --- Étape 1 : Reprojection de EPSG:4326 vers Lambert 93 (EPSG:2154) ---
    # Les données source sont en WGS84 (degrés décimaux) : les distances et longueurs
    # ne peuvent pas être calculées en mètres sans reprojection préalable.
    # Lambert 93 est la projection métrique officielle pour la France métropolitaine.
    arcpy.AddMessage("Étape 1/9 — Reprojection des données vers Lambert 93 (EPSG:2154)...")
    sr_lambert93 = arcpy.SpatialReference(2154)

    arcpy.management.Project(
        in_dataset=communes,
        out_dataset=tmp_communes_proj,
        out_coor_system=sr_lambert93
    )
    arcpy.management.Project(
        in_dataset=espaces_verts,
        out_dataset=tmp_ev_proj,
        out_coor_system=sr_lambert93
    )
    arcpy.management.Project(
        in_dataset=routes,
        out_dataset=tmp_routes_proj,
        out_coor_system=sr_lambert93
    )
    arcpy.AddMessage("   -> Reprojection terminée. Toutes les données sont maintenant en mètres.")

    # --- Étape 2 : Buffer autour des espaces verts (en mètres, grâce à Lambert 93) ---
    arcpy.AddMessage("Étape 2/9 — Création du buffer autour des espaces verts...")
    arcpy.analysis.Buffer(
        in_features=tmp_ev_proj,
        out_feature_class=tmp_buffer_ev,
        buffer_distance_or_field=f"{distance_buffer} Meters",
        dissolve_option="ALL"
    )

    # --- Étape 3 : Clip des routes par le buffer ---
    arcpy.AddMessage("Étape 3/9 — Découpage des routes par la zone tampon...")
    arcpy.analysis.Clip(
        in_features=tmp_routes_proj,
        clip_features=tmp_buffer_ev,
        out_feature_class=tmp_routes_clip
    )

    # --- Étape 4 : Intersection des routes clippées avec les communes ---
    arcpy.AddMessage("Étape 4/9 — Intersection des routes avec les communes...")
    arcpy.analysis.Intersect(
        in_features=[tmp_routes_clip, tmp_communes_proj],
        out_feature_class=tmp_routes_communes,
        join_attributes="ALL"
    )

    # --- Étape 5 : Calcul de la longueur réelle de chaque tronçon découpé ---
    # Le champ LENGTH existant dans la table routes correspond à la longueur
    # des tronçons originaux avant découpage — il ne peut pas être réutilisé.
    # On crée LONGUEUR_M recalculé géométriquement après Clip et Intersect,
    # en mètres (possible grâce à la reprojection en Lambert 93).
    arcpy.AddMessage("Étape 5/9 — Calcul des longueurs réelles des tronçons découpés (en mètres)...")
    arcpy.management.AddField(
        in_table=tmp_routes_communes,
        field_name="LONGUEUR_M",
        field_type="DOUBLE"
    )
    arcpy.management.CalculateGeometryAttributes(
        in_features=tmp_routes_communes,
        geometry_property=[["LONGUEUR_M", "LENGTH"]],
        length_unit="METERS"
    )

    # --- Étape 6 : Statistiques — somme des longueurs par commune ---
    arcpy.AddMessage("Étape 6/9 — Agrégation des longueurs par commune...")

    # Détection automatique du champ identifiant commune dans la couche intersectée.
    # Après Intersect, les champs des deux couches sont présents.
    # Ordre de priorité : insee (communes) > C_COINSEE (routes) > INSEE_COM > NOM_COM
    champs = [f.name for f in arcpy.ListFields(tmp_routes_communes)]
    champ_commune = None
    for candidat in ["insee", "C_COINSEE", "INSEE_COM", "NOM_COM", "nom"]:
        if candidat in champs:
            champ_commune = candidat
            break

    if champ_commune is None:
        arcpy.AddError("Aucun champ identifiant commune trouvé ('insee', 'C_COINSEE', 'INSEE_COM', 'NOM_COM').")
        raise SystemExit

    arcpy.AddMessage(f"   -> Champ identifiant commune utilisé : '{champ_commune}'")

    arcpy.analysis.Statistics(
        in_table=tmp_routes_communes,
        out_table=tmp_stats,
        statistics_fields=[["LONGUEUR_M", "SUM"]],
        case_field=champ_commune
    )

    # --- Étape 7 : Copie de la couche communes reprojetée vers la sortie ---
    # La couche de sortie est en Lambert 93 pour la cohérence des calculs métriques.
    arcpy.AddMessage("Étape 7/9 — Copie de la couche communes vers la sortie...")
    arcpy.management.CopyFeatures(
        in_features=tmp_communes_proj,
        out_feature_class=output_fc
    )

    # --- Étape 8 : Jointure de la table de statistiques sur la couche de sortie ---
    arcpy.AddMessage("Étape 8/9 — Jointure des statistiques sur la couche communes...")
    arcpy.management.JoinField(
        in_data=output_fc,
        in_field=champ_commune,
        join_table=tmp_stats,
        join_field=champ_commune,
        fields=["SUM_LONGUEUR_M"]
    )

    # --- Étape 9 : Remplacement des valeurs nulles par 0 ---
    # Les communes sans aucune route dans la zone tampon n'ont pas de ligne
    # dans la table de statistiques -> valeur nulle après jointure -> remplacée par 0.
    arcpy.AddMessage("Étape 9/9 — Remplacement des valeurs nulles par 0...")
    with arcpy.da.UpdateCursor(output_fc, ["SUM_LONGUEUR_M"]) as cursor:
        for row in cursor:
            if row[0] is None:
                row[0] = 0
                cursor.updateRow(row)

    # --- Nettoyage des fichiers intermédiaires ---
    arcpy.AddMessage("Nettoyage des fichiers intermédiaires...")
    for tmp in [tmp_communes_proj, tmp_ev_proj, tmp_routes_proj,
                tmp_buffer_ev, tmp_routes_clip, tmp_routes_communes, tmp_stats]:
        if arcpy.Exists(tmp):
            arcpy.management.Delete(tmp)

    arcpy.AddMessage("Traitement terminé avec succès.")
    arcpy.AddMessage(f"Résultat disponible dans : {output_fc}")
    arcpy.AddMessage("Projection de la couche de sortie : RGF93 / Lambert-93 (EPSG:2154)")

# =========================
# GESTION DES ERREURS
# =========================

# Erreurs propres à ArcGIS / ArcPy
except arcpy.ExecuteError:
    arcpy.AddError(arcpy.GetMessages(2))

# Autres erreurs Python
except Exception as e:
    arcpy.AddError(str(e))