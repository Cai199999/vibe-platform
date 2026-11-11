"""Babel AST 静态门禁：ORM 字段 / 表名 / 运行期变更回归测试。"""

from __future__ import annotations

import unittest

from ai_core.fullstack.coders import check_orm_schema, extract_orm_schema
from ai_core.fullstack.schema_ast import analyze_js_files

DB_OK = """const TABLE = 'resumes'
const COLUMN_DEFS = [['id', 'INTEGER'], ['file_name', 'TEXT']]
"""

DB_MUTATED = """const TABLE = 'resumes'
const COLUMN_DEFS = [['id', 'INTEGER'], ['file_name', 'TEXT']]
COLUMN_DEFS.push(['name', 'TEXT'])
"""

DB_DYNAMIC = """const TABLE = 'resumes'
const COLUMN_DEFS = JSON.parse(process.env.COLUMNS || '[]')
"""

DB_RELATED_OK = """const TABLE = 'resumes'
const COLUMN_DEFS = [['id', 'INTEGER'], ['user_id', 'INTEGER'], ['file_name', 'TEXT']]
const COLUMN_META = { id: {}, user_id: {}, file_name: {} }
const RELATED_MODELS = [
  {
    table: 'users',
    columns: ['id', 'name'],
    columnDefs: [['id', 'INTEGER'], ['name', 'TEXT']],
    columnMeta: { id: {}, name: {} },
  },
]
const RELATIONS = [
  {
    type: 'one_to_many',
    parentTable: 'users',
    childTable: 'resumes',
    foreignKey: 'user_id',
    parentKey: 'id',
  },
]
"""

DDL_TABLES = {
    "resumes": ("id", "user_id", "file_name"),
    "users": ("id", "name"),
}


class SchemaAstTest(unittest.TestCase):
    def test_babel_ast_extracts_table_and_columns(self) -> None:
        result = analyze_js_files(
            {
                "server/db.js": DB_OK,
                "server/index.js": "const TABLE = 'resumes'\n",
            }
        )
        db = result["server/db.js"]
        self.assertEqual(db.table, "resumes")
        self.assertEqual(db.columns, ("id", "file_name"))
        self.assertEqual(db.column_defs, (("id", "INTEGER"), ("file_name", "TEXT")))
        self.assertEqual(db.errors, ())

    def test_static_concat_and_json_parse_are_supported(self) -> None:
        source = (
            "const TABLE = 'res' + 'umes'\n"
            "const COLUMN_DEFS = JSON.parse("
            '\'[["id", "INTEGER"], ["file_name", "TEXT"]]\')\n'
        )
        db = analyze_js_files({"server/db.js": source})["server/db.js"]
        self.assertEqual(db.table, "resumes")
        self.assertEqual(db.columns, ("id", "file_name"))
        self.assertEqual(db.errors, ())

    def test_runtime_mutation_is_blocked(self) -> None:
        """旧正则只读声明，会漏掉 COLUMN_DEFS.push 追加的 name。"""
        db = analyze_js_files({"server/db.js": DB_MUTATED})["server/db.js"]
        self.assertEqual(db.table, "resumes")
        self.assertTrue(any("push" in error for error in db.errors))

        messages = check_orm_schema(
            {
                "server/db.js": DB_MUTATED,
                "server/index.js": "const TABLE = 'resumes'\n",
            },
            table="resumes",
            expected_columns=("id", "file_name"),
        )
        self.assertTrue(any("运行期" in message for message in messages))

    def test_dynamic_column_defs_are_blocked(self) -> None:
        db = analyze_js_files({"server/db.js": DB_DYNAMIC})["server/db.js"]
        self.assertEqual(db.columns, ())
        self.assertTrue(any("静态" in error for error in db.errors))

        messages = check_orm_schema(
            {"server/db.js": DB_DYNAMIC},
            table="resumes",
            expected_columns=("id", "file_name"),
        )
        self.assertTrue(any("COLUMN_DEFS" in message for message in messages))

    def test_syntax_error_is_reported_not_silently_passed(self) -> None:
        db = analyze_js_files(
            {"server/db.js": "const TABLE = 'resumes'\nconst = broken\n"}
        )["server/db.js"]
        self.assertTrue(any("语法错误" in error for error in db.errors))

    def test_define_table_name_and_attributes_are_extracted(self) -> None:
        source = """const TABLE = 'resumes'
const COLUMN_DEFS = [['id', 'INTEGER'], ['file_name', 'TEXT']]
const attributes = {
  id: { type: 'INTEGER' },
  file_name: { type: 'TEXT' },
}
const model = sequelize.define('Record', attributes, { tableName: TABLE })
"""
        db = analyze_js_files({"server/db.js": source})["server/db.js"]
        self.assertEqual(db.table, "resumes")
        self.assertEqual(db.define_tables, ("resumes",))
        self.assertEqual(db.attribute_columns, ("id", "file_name"))
        self.assertEqual(db.errors, ())

    def test_table_mismatch_and_unknown_column_still_reported(self) -> None:
        bad = """const TABLE = 'users'
const COLUMN_DEFS = [['id', 'INTEGER'], ['name', 'TEXT']]
"""
        messages = check_orm_schema(
            {
                "server/db.js": bad,
                "server/index.js": "const TABLE = 'users'\n",
            },
            table="resumes",
            expected_columns=("id", "file_name", "content"),
        )
        combined = " ".join(messages)
        self.assertIn("users", combined)
        self.assertIn("name", combined)
        self.assertIn("index.js", combined)

    def test_extract_orm_schema_is_ast_backed(self) -> None:
        table, columns = extract_orm_schema(DB_OK)
        self.assertEqual(table, "resumes")
        self.assertEqual(columns, ["id", "file_name"])

    def test_related_models_and_relations_are_extracted(self) -> None:
        db = analyze_js_files({"server/db.js": DB_RELATED_OK})["server/db.js"]
        self.assertEqual(db.errors, ())
        self.assertEqual(
            [(model.table, model.columns) for model in db.related_models],
            [("users", ("id", "name"))],
        )
        self.assertEqual(len(db.relations), 1)
        relation = db.relations[0]
        self.assertEqual(relation.parent_table, "users")
        self.assertEqual(relation.child_table, "resumes")
        self.assertEqual(relation.foreign_key, "user_id")
        self.assertEqual(
            db.table_columns,
            {
                "resumes": ("id", "user_id", "file_name"),
                "users": ("id", "name"),
            },
        )

    def test_related_model_unknown_column_is_blocked(self) -> None:
        """关联表 include 查询未知字段，和核心表一样必须阻断。"""
        bad = (
            DB_RELATED_OK.replace(
                "columns: ['id', 'name']", "columns: ['id', 'iam_sub']"
            )
            .replace("['name', 'TEXT']", "['iam_sub', 'TEXT']")
            .replace(
                "columnMeta: { id: {}, name: {} }",
                "columnMeta: { id: {}, iam_sub: {} }",
            )
        )
        messages = check_orm_schema(
            {"server/db.js": bad},
            table="resumes",
            expected_columns=DDL_TABLES["resumes"],
            expected_tables=DDL_TABLES,
        )
        combined = " ".join(messages)
        self.assertIn("users", combined)
        self.assertIn("iam_sub", combined)

    def test_related_model_runtime_mutation_is_blocked(self) -> None:
        mutated = DB_RELATED_OK + (
            "\nRELATED_MODELS.push({ table: 'jds', columns: ['id', 'title'] })\n"
        )
        db = analyze_js_files({"server/db.js": mutated})["server/db.js"]
        self.assertTrue(any("RELATED_MODELS" in error for error in db.errors))
        messages = check_orm_schema(
            {"server/db.js": mutated},
            table="resumes",
            expected_columns=DDL_TABLES["resumes"],
            expected_tables=DDL_TABLES,
        )
        self.assertTrue(any("运行期" in message for message in messages))

    def test_related_model_without_relations_is_blocked(self) -> None:
        """声明了关联模型却不生成 hasMany / belongsTo，等于退回单表 CRUD。"""
        source = DB_RELATED_OK.replace(
            """const RELATIONS = [
  {
    type: 'one_to_many',
    parentTable: 'users',
    childTable: 'resumes',
    foreignKey: 'user_id',
    parentKey: 'id',
  },
]
""",
            "const RELATIONS = []\n",
        )
        messages = check_orm_schema(
            {"server/db.js": source},
            table="resumes",
            expected_columns=DDL_TABLES["resumes"],
            expected_tables=DDL_TABLES,
        )
        self.assertTrue(any("RELATIONS" in message for message in messages))

    def test_relation_foreign_key_must_exist_in_child(self) -> None:
        bad = DB_RELATED_OK.replace("foreignKey: 'user_id'", "foreignKey: 'owner_id'")
        db = analyze_js_files({"server/db.js": bad})["server/db.js"]
        self.assertTrue(any("owner_id" in error for error in db.errors))


if __name__ == "__main__":
    unittest.main()
