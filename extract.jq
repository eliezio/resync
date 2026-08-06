# Flatten a SchemaCrawler `serialize` JSON catalog into a re-sync config skeleton.
# Handles XStream wrapping (lists serialized as ["java.util...", [items]]) and
# uuid object references (first occurrence = full object, later = bare uuid string).

# Unwrap a possibly class-wrapped list into a plain array.
def L: if (type == "array" and (.[0] | type) == "string") then .[1] else . end;

# uuid -> column facts, from the fully-resolved column objects.
( [ .. | objects | select(has("part-of-primary-key")) ]
  | map({ (.["@uuid"]): {
        table:    (.["full-name"] | split(".")[1]),
        column:   .name,
        nullable: .nullable,
        in_pk:    .["part-of-primary-key"],
        in_fk:    .["part-of-foreign-key"],
        in_uniq:  .["part-of-unique-index"]
      } })
  | add ) as $col

# Distinct foreign keys (dedup by FK name; the graph is inlined many times).
| ( [ .. | objects | select(has("column-references") and has("foreign-key-table")) ]
    | group_by(.name) | map(.[0])
    | map({
        name,
        nullable: .optional,                       # FK column(s) nullable?
        pairs: [ (.["column-references"] | L)[]
                 | { child:  $col[.["foreign-key-column"]],
                     parent: $col[.["primary-key-column"]] } ]
      }) ) as $fks

# Group columns by their table.
| ( [ $col[] ] | group_by(.table)
    | map({ (.[0].table): . }) | add ) as $bytable

| {
    edges: [ $fks[] | . as $fk
             | { fk: $fk.name, nullable: $fk.nullable,
                 child:  ($fk.pairs[0].child.table),
                 parent: ($fk.pairs[0].parent.table),
                 columns: [ $fk.pairs[] | "\(.child.column)->\(.parent.column)" ] } ],

    tables: [ $bytable | to_entries[] | {
        name: .key,
        columns:  [ .value[] | {name: .column, nullable, in_pk, in_fk, in_uniq} ],
        pk_cols:  [ .value[] | select(.in_pk)   | .column ],
        uniq_candidate_cols: [ .value[] | select(.in_uniq and (.in_pk|not)) | .column ],
        self_referencing: false,
        # to fill in by hand, using the decision matrix:
        identity_mode: "TODO natural|value|hash|reload|out_of_scope",
        identity_columns: [],
        hash_exclude: []
      } ]
  }
