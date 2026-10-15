# ChatGPT prompt: generate a MyFitnessPal weekly meal plan

Copy everything below the line into ChatGPT. Replace the goals/preferences in
step 1 with your own. Save ChatGPT's JSON output as `plan.json`.

----------------------------------------------------------------------

You are a meal-planning assistant. Produce a 7-day meal plan as a single JSON
document that conforms EXACTLY to the JSON Schema at the end of this message.

My preferences (edit these):
- Goal: ~2000 kcal/day, ~150 g protein
- Diet: no restrictions
- Week starting: 2026-10-19 (Monday)
- Meals: breakfast, lunch, dinner, and 1-2 snacks per day

Rules you MUST follow:
- Output ONLY the JSON. No prose, no markdown fences.
- "schema_version" is 1.
- One object per day in "days"; "date" is ISO "YYYY-MM-DD".
- "meals" keys are exactly: breakfast, lunch, dinner, snack (lowercase).
- Every food item needs a UNIQUE "entry_id" (kebab-case, e.g. "mon-breakfast-oats"),
  a plain-English "food" name, a numeric "quantity" > 0, and a "unit".
- Prefer "g" for solids; use a natural serving unit (e.g. "medium", "cup", "slice")
  only when that's how the food is normally measured.
- Include "expected_calories" for each item (your best estimate) so the tool can
  flag mismatches. Add "expected_macros" {protein_g, carbs_g, fat_g} if you can.
- DO NOT invent MyFitnessPal food IDs. Leave "mfp_food_id" out entirely.

JSON Schema (your output must validate against this):

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
