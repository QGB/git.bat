import ast
import unittest
from pathlib import Path

from git.generate_single_file import assemble, source_stamp


ROOT = Path(__file__).resolve().parents[2]


class SingleFileGenerationTests(unittest.TestCase):
    def test_generated_functions_match_the_original_script(self):
        original = ast.parse((ROOT / "git_logic.py").read_text(encoding="utf-8"))
        generated_source = assemble()
        generated = ast.parse(generated_source)
        original_functions = {
            node.name: node
            for node in original.body
            if isinstance(node, ast.FunctionDef)
        }
        generated_functions = {
            node.name: node
            for node in generated.body
            if isinstance(node, ast.FunctionDef)
        }

        self.assertEqual(set(generated_functions), set(original_functions))
        for name, original_function in original_functions.items():
            self.assertEqual(
                ast.dump(generated_functions[name], include_attributes=False),
                ast.dump(original_function, include_attributes=False),
                name,
            )

        namespace = {"__name__": "generated_git_logic", "__file__": str(ROOT / "generated.py")}
        exec(compile(generated_source, namespace["__file__"], "exec"), namespace)
        self.assertEqual(namespace["parse_size_str"]("1.5mb"), 1_572_864)
        self.assertEqual(
            namespace["parse_github_subdirectory_url"](
                "https://github.com/acme/demo/tree/dev/mobile"
            ),
            ("https://github.com/acme/demo.git", "dev", "mobile"),
        )

    def test_generated_stamp_matches_sources(self):
        generated = ast.parse(assemble())
        constants = {}
        for node in generated.body:
            if not (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)):
                continue
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
        self.assertEqual(constants["GENERATED_SOURCE_STAMP"], source_stamp())
        self.assertIn("core.py", constants["GENERATED_SOURCE_FILES"])
        self.assertIn("generate_single_file.py", constants["GENERATED_SOURCE_FILES"])


if __name__ == "__main__":
    unittest.main()
