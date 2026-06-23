# Rapport de Test - Sauce Demo - Swag Labs
**Application URL:** https://www.saucedemo.com/
**Date du rapport:** 07/05/2026 à 10:39 UTC
**Généré par:** omniAgent

---

## Notes de l'analyste
Application de démonstration e‑commerce avec authentification, catalogue produits, panier, checkout et menu latéral. Aucun endpoint backend visible, mais les flux CRUD sont implémentés côté client.

## Tableau récapitulatif des scénarios

| ID | Titre | Type | Priorité | Étapes |
|---|---|---|---|---|
| TC-001 | Login réussi avec un utilisateur standard | authentification | HAUTE | 3 |
| TC-002 | Login échoué avec identifiants invalides | erreur | HAUTE | 3 |
| TC-003 | Login bloqué pour l'utilisateur locked_out_user | erreur | HAUTE | 3 |
| TC-004 | Logout depuis le menu latéral | navigation | MOYENNE | 2 |
| TC-005 | Affichage de la liste complète des produits (Read) | formulaire | HAUTE | 2 |
| TC-006 | Tri des produits par prix croissant | formulaire | MOYENNE | 3 |
| TC-007 | Tri des produits par nom décroissant (Z à A) | formulaire | MOYENNE | 3 |
| TC-008 | Ajout d'un produit au panier depuis la liste (Create) | nominal | HAUTE | 3 |
| TC-009 | Ajout d'un produit au panier depuis la fiche produit (Create) | nominal | MOYENNE | 3 |
| TC-010 | Modification de la quantité d'un produit dans le panier (Update) | nominal | MOYENNE | 2 |
| TC-011 | Tentative de checkout avec champ prénom vide (Erreur formulaire) | erreur | HAUTE | 3 |
| TC-012 | Checkout complet avec informations valides (Happy path) | nominal | HAUTE | 4 |
| TC-013 | Accès direct à la page panier sans authentification (Access control) | accessibilite | HAUTE | 1 |
| TC-014 | Ouverture et fermeture du menu latéral depuis la page produit | navigation | MOYENNE | 2 |
| TC-015 | Navigation vers la page "All Items" via le menu latéral | navigation | MOYENNE | 2 |
| TC-016 | Réinitialisation de l'état de l'application via le menu | nominal | BASSE | 3 |
| TC-017 | Comportement du compte "performance_glitch_user" (Performance edge case) | performance | BASSE | 3 |
| TC-018 | Vérification du badge du panier lors de l'ajout de plusieurs produits | nominal | MOYENNE | 2 |
| TC-019 | Accès à la page "About" via le menu latéral | navigation | BASSE | 2 |
| TC-020 | Tentative d'ajout d'un produit déjà présent dans le panier (Idempotence) | limite | MOYENNE | 1 |

## Détail des scénarios

### TC-001 - Login réussi avec un utilisateur standard
- **Type:** authentification
- **Priorité:** HAUTE

#### Préconditions
- L'application est ouverte sur la page de login

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Saisir "standard_user" dans le champ Username | Le texte "standard_user" apparaît dans le champ |
| 2 | Saisir "secret_sauce" dans le champ Password | Le texte masqué "secret_sauce" apparaît dans le champ |
| 3 | Cliquer sur le bouton "Login" | L'utilisateur est redirigé vers la page "Products" et le menu latéral est disponible |

**Résultat global attendu:** Connexion réussie, affichage de la liste des produits

**Données de test suggérées:** `standard_user / secret_sauce`

**Tags:** `#login`, `#happy_path`, `#auth`

---

### TC-002 - Login échoué avec identifiants invalides
- **Type:** erreur
- **Priorité:** HAUTE

#### Préconditions
- L'application est ouverte sur la page de login

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Saisir "invalid_user" dans le champ Username | Le texte "invalid_user" apparaît dans le champ |
| 2 | Saisir "wrong_pass" dans le champ Password | Le texte masqué "wrong_pass" apparaît dans le champ |
| 3 | Cliquer sur le bouton "Login" | Un message d'erreur "Epic sadface: Username and password do not match any user in this service" s'affiche |

**Résultat global attendu:** Connexion refusée, l'utilisateur reste sur la page de login

**Données de test suggérées:** `invalid_user / wrong_pass`

**Tags:** `#login`, `#negative`, `#auth`

---

### TC-003 - Login bloqué pour l'utilisateur locked_out_user
- **Type:** erreur
- **Priorité:** HAUTE

#### Préconditions
- L'application est ouverte sur la page de login

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Saisir "locked_out_user" dans le champ Username | Le texte "locked_out_user" apparaît dans le champ |
| 2 | Saisir "secret_sauce" dans le champ Password | Le texte masqué "secret_sauce" apparaît dans le champ |
| 3 | Cliquer sur le bouton "Login" | Un message d'erreur "Epic sadface: Sorry, this user has been locked out." s'affiche |

**Résultat global attendu:** Connexion refusée, l'utilisateur reste sur la page de login

**Données de test suggérées:** `locked_out_user / secret_sauce`

**Tags:** `#login`, `#locked_user`, `#auth`

---

### TC-004 - Logout depuis le menu latéral
- **Type:** navigation
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté (standard_user) sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du menu (burger) en haut à gauche | Le menu latéral s'ouvre |
| 2 | Cliquer sur le lien "Logout" dans le menu | L'utilisateur est redirigé vers la page de login |

**Résultat global attendu:** Session terminée, retour à l'écran de connexion

**Tags:** `#logout`, `#navigation`, `#auth`

---

### TC-005 - Affichage de la liste complète des produits (Read)
- **Type:** formulaire
- **Priorité:** HAUTE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Vérifier que la page affiche un titre "Products" | Le titre "Products" est visible |
| 2 | Compter le nombre d'éléments avec la classe "inventory_item" | Le nombre correspond au catalogue (6 produits) |

**Résultat global attendu:** Tous les produits sont listés avec image, nom, description et prix

**Tags:** `#read`, `#catalogue`

---

### TC-006 - Tri des produits par prix croissant
- **Type:** formulaire
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le menu déroulant "Sort By" | Le menu s'ouvre |
| 2 | Sélectionner l'option "Price (low to high)" | La liste des produits se réordonne |
| 3 | Lire le prix du premier produit affiché | Le prix correspond au plus bas du catalogue (7.99) |

**Résultat global attendu:** Produits affichés du moins cher au plus cher

**Tags:** `#sorting`, `#price`

---

### TC-007 - Tri des produits par nom décroissant (Z à A)
- **Type:** formulaire
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le menu déroulant "Sort By" | Le menu s'ouvre |
| 2 | Sélectionner l'option "Name (Z to A)" | La liste des produits se réordonne |
| 3 | Lire le nom du premier produit affiché | Le nom commence par la lettre la plus tardive (ex. "Test.allTheThings() T-Shirt (Red)") |

**Résultat global attendu:** Produits affichés du Z vers le A

**Tags:** `#sorting`, `#name`

---

### TC-008 - Ajout d'un produit au panier depuis la liste (Create)
- **Type:** nominal
- **Priorité:** HAUTE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le bouton "Add to cart" du produit "Sauce Labs Backpack" | Le bouton change en "Remove" et le badge du panier affiche "1" |
| 2 | Cliquer sur l'icône du panier en haut à droite | L'utilisateur est redirigé vers la page "Your Cart" |
| 3 | Vérifier que le produit "Sauce Labs Backpack" apparaît dans le tableau du panier | Le produit est listé avec la bonne quantité (1) et le bon prix |

**Résultat global attendu:** Produit ajouté au panier et visible dans le panier

**Tags:** `#add_to_cart`, `#cart`

---

### TC-009 - Ajout d'un produit au panier depuis la fiche produit (Create)
- **Type:** nominal
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le nom du produit "Sauce Labs Bike Light" | La page de détail du produit s'ouvre |
| 2 | Cliquer sur le bouton "Add to cart" | Le bouton change en "Remove" et le badge du panier passe à "1" |
| 3 | Cliquer sur l'icône du panier | Redirection vers la page "Your Cart" avec le produit listé |

**Résultat global attendu:** Produit ajouté depuis la page détail et présent dans le panier

**Tags:** `#add_to_cart`, `#detail_page`

---

### TC-010 - Modification de la quantité d'un produit dans le panier (Update)
- **Type:** nominal
- **Priorité:** MOYENNE

#### Préconditions
- Produit déjà ajouté au panier (ex. Sauce Labs Backpack)

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du panier pour ouvrir la page "Your Cart" | Le panier affiche le produit avec un bouton "Remove" |
| 2 | Cliquer sur le bouton "Remove" du produit | Le produit disparaît du tableau et le badge du panier devient vide ou "0" |

**Résultat global attendu:** Produit retiré du panier, mise à jour du compteur

**Tags:** `#remove_from_cart`, `#update`

---

### TC-011 - Tentative de checkout avec champ prénom vide (Erreur formulaire)
- **Type:** erreur
- **Priorité:** HAUTE

#### Préconditions
- Panier contenant au moins un produit, utilisateur sur la page "Your Cart"

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le bouton "Checkout" | Redirection vers la page "Checkout: Your Information" |
| 2 | Laisser le champ "First Name" vide, saisir "Doe" dans "Last Name" et "12345" dans "Postal Code" | Les champs sont remplis sauf le prénom |
| 3 | Cliquer sur le bouton "Continue" | Un message d'erreur "Error: First Name is required" s'affiche |

**Résultat global attendu:** Le processus de checkout est bloqué tant que le prénom est vide

**Tags:** `#checkout`, `#validation`, `#negative`

---

### TC-012 - Checkout complet avec informations valides (Happy path)
- **Type:** nominal
- **Priorité:** HAUTE

#### Préconditions
- Panier contenant au moins un produit, utilisateur sur la page "Your Cart"

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur le bouton "Checkout" | Redirection vers la page "Checkout: Your Information" |
| 2 | Saisir "John" dans le champ "First Name", "Doe" dans "Last Name" et "90210" dans "Postal Code" | Les trois champs sont remplis |
| 3 | Cliquer sur le bouton "Continue" | Redirection vers la page "Checkout: Overview" affichant le récapitulatif |
| 4 | Cliquer sur le bouton "Finish" | Affichage du message de confirmation "THANK YOU FOR YOUR ORDER" |

**Résultat global attendu:** Commande finalisée avec succès et page de confirmation affichée

**Tags:** `#checkout`, `#happy_path`

---

### TC-013 - Accès direct à la page panier sans authentification (Access control)
- **Type:** accessibilite
- **Priorité:** HAUTE

#### Préconditions
- Navigateur ouvert sur une nouvelle fenêtre, aucune session active

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Saisir l'URL https://www.saucedemo.com/cart.html dans la barre d'adresse | Le serveur redirige vers la page de login |

**Résultat global attendu:** L'utilisateur non authentifié ne peut pas voir le panier et est renvoyé à la page de login

**Tags:** `#security`, `#auth`

---

### TC-014 - Ouverture et fermeture du menu latéral depuis la page produit
- **Type:** navigation
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du menu (burger) en haut à gauche | Le menu latéral s'ouvre |
| 2 | Cliquer sur l'icône de fermeture (X) du menu | Le menu latéral se referme |

**Résultat global attendu:** Menu latéral s'ouvre et se ferme correctement

**Tags:** `#menu`, `#navigation`

---

### TC-015 - Navigation vers la page "All Items" via le menu latéral
- **Type:** navigation
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Checkout Overview

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du menu | Le menu s'ouvre |
| 2 | Cliquer sur le lien "All Items" | Redirection vers la page "Products" |

**Résultat global attendu:** L'utilisateur revient à la liste complète des produits

**Tags:** `#navigation`, `#menu`

---

### TC-016 - Réinitialisation de l'état de l'application via le menu
- **Type:** nominal
- **Priorité:** BASSE

#### Préconditions
- Produit ajouté au panier, utilisateur connecté

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du menu | Le menu s'ouvre |
| 2 | Cliquer sur le bouton "Reset App State" | Le panier est vidé, le badge du panier disparaît |
| 3 | Vérifier que le bouton "Add to cart" est de nouveau affiché pour le produit précédemment ajouté | Le bouton redevient "Add to cart" |

**Résultat global attendu:** L'application revient à son état initial sans rafraîchir la page

**Tags:** `#reset`, `#state`

---

### TC-017 - Comportement du compte "performance_glitch_user" (Performance edge case)
- **Type:** performance
- **Priorité:** BASSE

#### Préconditions
- L'application est ouverte sur la page de login

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Saisir "performance_glitch_user" dans le champ Username | Le texte apparaît |
| 2 | Saisir "secret_sauce" dans le champ Password | Le texte apparaît |
| 3 | Cliquer sur le bouton "Login" | Le chargement de la page Products prend plus de 5 secondes |

**Résultat global attendu:** L'utilisateur est connecté mais la page met un temps de chargement notable

**Tags:** `#performance`, `#glitch_user`

---

### TC-018 - Vérification du badge du panier lors de l'ajout de plusieurs produits
- **Type:** nominal
- **Priorité:** MOYENNE

#### Préconditions
- Utilisateur connecté sur la page Products

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur "Add to cart" du produit "Sauce Labs Backpack" | Badge du panier affiche "1" |
| 2 | Cliquer sur "Add to cart" du produit "Sauce Labs Bike Light" | Badge du panier affiche "2" |

**Résultat global attendu:** Le compteur du panier reflète exactement le nombre d'articles ajoutés

**Tags:** `#cart`, `#badge`

---

### TC-019 - Accès à la page "About" via le menu latéral
- **Type:** navigation
- **Priorité:** BASSE

#### Préconditions
- Utilisateur connecté sur n'importe quelle page

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer sur l'icône du menu | Le menu s'ouvre |
| 2 | Cliquer sur le lien "About" | Redirection vers la page "https://saucelabs.com/" (ou page About intégrée) |

**Résultat global attendu:** L'utilisateur voit la page About

**Tags:** `#navigation`, `#about`

---

### TC-020 - Tentative d'ajout d'un produit déjà présent dans le panier (Idempotence)
- **Type:** limite
- **Priorité:** MOYENNE

#### Préconditions
- Produit "Sauce Labs Backpack" déjà ajouté au panier

#### Étapes du test
| N° | Action | Résultat attendu |
|---|---|---|
| 1 | Cliquer de nouveau sur le bouton "Add to cart" du même produit | Le bouton reste "Remove" et le badge du panier reste à "1" |

**Résultat global attendu:** Pas de duplication, le panier conserve une seule entrée du produit

**Tags:** `#cart`, `#idempotence`

---

## Recommandations techniques
- Automatiser les scénarios de login, tri et checkout avec un framework Selenium ou Cypress.
- Inclure des tests de performance pour le compte performance_glitch_user afin de valider les seuils de temps de chargement.
- Vérifier la compatibilité mobile du menu latéral et du panier.
- Ajouter des tests de sécurité pour s'assurer que les URLs protégées renvoient bien vers la page de login.
- Mettre en place des tests de régression visuelle pour détecter les changements d'affichage des produits et du badge du panier.

## Conclusion
L'analyse automatisée a permis de couvrir les flux principaux de l'application. Ce rapport fournit une base pour la validation fonctionnelle.

*Fin du rapport - Généré le 07/05/2026 à 10:39 UTC*


{
  "base_url": "https://www.saucedemo.com",
  "md_content": "## Fonctionnalité\nAuthentification sur Saucedemo.\n\n## Exigences fonctionnelles\nL'utilisateur doit pouvoir se connecter avec le nom d'utilisateur 'standard_user' et le mot de passe 'secret_sauce'. Après la connexion, il doit atterrir sur la page d'inventaire des produits."
}