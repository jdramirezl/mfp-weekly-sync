# ChatGPT prompt: generate a MyFitnessPal meal plan

This prompt is tuned so the JSON you get back **resolves cleanly** against
MyFitnessPal's food database. The output validates against
[`weekly_plan.schema.json`](weekly_plan.schema.json).

> Status: the legacy `mfp-sync` CLI that consumed these plans was removed (see
> the `legacy-mfp-sync` git tag). The schema and prompt are kept as the input
> contract for the planned v2 weekly-plan workflow in assistant-hub, which will
> log plans through Mason's draft → confirm tools following
> [`docs/reconciliation-rules.md`](../docs/reconciliation-rules.md).

----------------------------------------------------------------------

You generate meal plans as a single JSON document for a tool called
`mfp-weekly-sync`, which logs them into MyFitnessPal. The tool matches each
food you name against MyFitnessPal's database, so HOW you name and quantify
foods decides whether it matches correctly. Follow every rule below.

## My plan request (EDIT THIS BLOCK)
- Dates: 2026-10-19 to 2026-10-23 (one object per day)
- Meals to fill: breakfast, lunch, dinner, snack  (leave others as empty lists)
- Daily target: ~2000 kcal, ~150 g protein
- Diet / preferences: (none / write yours here)
- Notes: (e.g. "Colombian breakfasts", "meal-prep the same lunch Mon-Tue")

## Output rules (MANDATORY)
1. Output ONLY the JSON object. No prose, no explanations, no ```json fences.
2. `schema_version` is 1. Set `timezone` to "America/Mexico_City".
3. One object per day in `days`; `date` is ISO "YYYY-MM-DD", a real calendar date.
4. `meals` keys are exactly: `breakfast`, `lunch`, `dinner`, `snack` (lowercase).
   Include all four every day; use an empty list `[]` for meals with no food.
5. Every food item has: a UNIQUE `entry_id` (kebab-case, e.g.
   "2026-10-19-breakfast-oats"), a `food` name, a numeric `quantity` > 0, a
   `unit`, and `expected_calories` (REQUIRED — see rule 9).

## How to NAME foods so they match (this is the important part)
6. Use SHORT, GENERIC, SINGULAR names in the USDA/common style — NOT long
   descriptions. The matcher rejects a candidate when your name is too elaborate.
   - GOOD: "Boiled potatoes", "Chicken breast, cooked", "White rice, cooked",
     "Rotini pasta, cooked", "Cheddar cheese, shredded", "White onion, raw",
     "Banana, raw", "Olive oil", "Tomato sauce".
   - BAD: "Arepa made with Harina PAN and water, prepared (120 g hydrated)",
     "Onion, cilantro and mixed vegetables", "Clight prepared sugar-free drink".
   - State the cooked/raw state when it changes calories ("cooked" vs "raw"),
     because they are different database foods.
7. SPLIT composite dishes into their component single foods, each its own item.
   A taco = tortilla + meat + cheese + onion + lime, as separate entries. Do not
   write "tacos al pastor" as one line.
8. Pick a UNIT the food is normally measured in:
   - Solids: `g` (grams). This is the most reliable unit — prefer it.
   - Liquids/drinks: `ml`.
   - Whole items counted individually: `piece` (eggs, tortillas, slices of bread,
     whole fruit). Say "large"/"medium" in the name if size matters, e.g.
     "Egg, whole, large".
   - Avoid cups/tbsp/"scoop"/"serving" unless that is genuinely the only natural
     measure — gram amounts resolve far more reliably.
9. `expected_calories` is REQUIRED on every item and must be your best honest
   estimate for THAT quantity (not per 100 g). The tool compares it to the
   matched food and WARNS on a big mismatch — this is how wrong matches get
   caught, so make the estimate realistic. Also include `expected_macros`
   `{ "protein_g": N, "carbs_g": N, "fat_g": N }` for that quantity.
10. If you know a MyFitnessPal food id for an item, you MAY add `mfp_food_id`
    (and `serving_id`) to force an exact match. If you are not certain, OMIT
    them — never guess an id.
11. Optional `brand` only when the product is genuinely branded and you mean
    that specific product.

## Sanity checks before you answer
- Each day's summed `expected_calories` is near my daily target.
- Every `entry_id` is unique across the WHOLE plan.
- Every item has `food`, `quantity`, `unit`, and `expected_calories`.
- Food names are short and generic (re-read rule 6).

Produce the plan now. Output only the JSON.

----------------------------------------------------------------------

## JSON Schema (your output MUST validate against this)

```json
{
  "$defs": {
    "DayPlan": {
      "additionalProperties": false,
      "description": "One calendar day with its four meal buckets.",
      "properties": {
        "date": {
          "format": "date",
          "title": "Date",
          "type": "string"
        },
        "meals": {
          "additionalProperties": {
            "items": {
              "$ref": "#/$defs/PlanEntry"
            },
            "type": "array"
          },
          "propertyNames": {
            "$ref": "#/$defs/MealCategory"
          },
          "title": "Meals",
          "type": "object"
        }
      },
      "required": [
        "date"
      ],
      "title": "DayPlan",
      "type": "object"
    },
    "Macros": {
      "additionalProperties": false,
      "description": "Optional per-entry nutrition the plan author expects (for validation only).\n\nThese are the *planned* values. They are kept strictly separate from what\nMyFitnessPal actually stores for the matched food (see nutrition.py).",
      "properties": {
        "calories": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Calories"
        },
        "protein_g": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Protein G"
        },
        "carbs_g": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Carbs G"
        },
        "fat_g": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Fat G"
        }
      },
      "title": "Macros",
      "type": "object"
    },
    "MealCategory": {
      "description": "The four meal categories the free MyFitnessPal tier exposes.\n\nMyFitnessPal's API uses capitalized names (Breakfast/Lunch/Dinner/Snacks).\nWe normalize to lowercase internally and map at the backend boundary.",
      "enum": [
        "breakfast",
        "lunch",
        "dinner",
        "snack"
      ],
      "title": "MealCategory",
      "type": "string"
    },
    "PlanEntry": {
      "additionalProperties": false,
      "description": "A single planned food item within a meal.",
      "properties": {
        "entry_id": {
          "description": "Stable ID unique within the plan.",
          "minLength": 1,
          "title": "Entry Id",
          "type": "string"
        },
        "food": {
          "description": "Display name used for searching/matching.",
          "minLength": 1,
          "title": "Food",
          "type": "string"
        },
        "quantity": {
          "description": "Amount in `unit`.",
          "exclusiveMinimum": 0,
          "title": "Quantity",
          "type": "number"
        },
        "unit": {
          "default": "serving",
          "description": "Measurement unit.",
          "title": "Unit",
          "type": "string"
        },
        "mfp_food_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Mfp Food Id"
        },
        "serving_id": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Serving Id"
        },
        "serving_description": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Serving Description"
        },
        "brand": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Brand"
        },
        "expected_calories": {
          "anyOf": [
            {
              "minimum": 0,
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Expected Calories"
        },
        "expected_macros": {
          "anyOf": [
            {
              "$ref": "#/$defs/Macros"
            },
            {
              "type": "null"
            }
          ],
          "default": null
        }
      },
      "required": [
        "entry_id",
        "food",
        "quantity"
      ],
      "title": "PlanEntry",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "description": "The top-level weekly plan document.",
  "properties": {
    "schema_version": {
      "default": 1,
      "title": "Schema Version",
      "type": "integer"
    },
    "timezone": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Timezone"
    },
    "plan_id": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "description": "Stable identifier for this plan. Derived from the filename if omitted.",
      "title": "Plan Id"
    },
    "days": {
      "items": {
        "$ref": "#/$defs/DayPlan"
      },
      "minItems": 1,
      "title": "Days",
      "type": "array"
    }
  },
  "title": "MyFitnessPal Weekly Meal Plan",
  "type": "object",
  "$schema": "https://json-schema.org/draft/2020-12/schema"
}
```
