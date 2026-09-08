"""ERP（SAP FI + SD/O2C）本体接入不变量：模块可解析、类归位到正确的 disjoint 顶层、
实例经 migrate 落进 current.ttl，且不触碰 common.ttl 的 AllDisjointClasses 红线。

纯静态校验既有产物 + 对本体模块图做结构推导，不跑重型 migrate、无副作用。
"""
from __future__ import annotations

import unittest
from pathlib import Path

try:
    from rdflib import Graph, Namespace, RDF, RDFS, OWL, URIRef
except ModuleNotFoundError:  # 依赖检查由 semantic_validate.py 强制；单测收集阶段可跳过
    Graph = None


ROOT = Path(__file__).resolve().parents[2]
MODULES_DIR = ROOT / "ontology" / "modules"
CURRENT_TTL = ROOT / "knowledge" / "semantic" / "current.ttl"
SEMI = Namespace("urn:pxai:semi:") if Graph else None

# common.ttl 的三顶层互斥类（AllDisjointClasses）——ERP 类必须干净归入其一，绝不双挂
PHYSICAL = URIRef("urn:pxai:semi:PhysicalEntity") if Graph else None
EVENT = URIRef("urn:pxai:semi:Event") if Graph else None
INFORMATION = URIRef("urn:pxai:semi:InformationEntity") if Graph else None


def _schema() -> "Graph":
    graph = Graph()
    for path in sorted(MODULES_DIR.glob("*.ttl")):
        graph.parse(path, format="turtle")
    return graph


def _ancestors(graph: "Graph", cls: URIRef) -> set:
    """沿 rdfs:subClassOf 上溯全部祖先（含去环）。"""
    seen: set = set()
    frontier = [cls]
    while frontier:
        nxt = []
        for node in frontier:
            for parent in graph.objects(node, RDFS.subClassOf):
                if isinstance(parent, URIRef) and parent not in seen:
                    seen.add(parent)
                    nxt.append(parent)
        frontier = nxt
    return seen


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class ErpOntologyModuleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _schema()

    def test_erp_modules_parse_and_declare_domain_roots(self) -> None:
        """两模块可解析，且各自声明领域根类（挂 InformationEntity），供读侧 subClassOf 归属增强。"""
        classes = set(self.schema.subjects(RDF.type, OWL.Class))
        self.assertIn(SEMI.FinancialArtifact, classes)
        self.assertIn(SEMI.SalesArtifact, classes)
        # 领域根挂在 InformationEntity 之下（账务/单据是信息实体，非物理实体、非事件）
        self.assertIn(INFORMATION, set(self.schema.objects(SEMI.FinancialArtifact, RDFS.subClassOf)))
        self.assertIn(INFORMATION, set(self.schema.objects(SEMI.SalesArtifact, RDFS.subClassOf)))

    def test_financial_objects_are_information_not_event(self) -> None:
        """账务对象类（科目/凭证/应收/合同负债/单据…）上溯到 InformationEntity，绝不落进 Event/PhysicalEntity。

        注：CompanyCode 例外——它是 SAP 法人主体，故意 subClassOf Organization（直接挂共享超类
        Entity、在三互斥顶层之外），单独由 test_company_code_is_organization 守护。
        """
        for name in ("GLAccount", "AccountingDocument", "ChartOfAccounts",
                     "SalesOrder", "BillingDocument", "AccountsReceivable", "ContractLiability"):
            cls = SEMI[name]
            anc = _ancestors(self.schema, cls)
            self.assertIn(INFORMATION, anc, f"{name} 应上溯到 InformationEntity")
            self.assertNotIn(EVENT, anc, f"{name} 不应是 Event")
            self.assertNotIn(PHYSICAL, anc, f"{name} 不应是 PhysicalEntity")

    def test_company_code_is_organization(self) -> None:
        """CompanyCode = SAP 法人记账主体，建模为 Organization（挂共享超类 Entity、在三互斥顶层之外），
        故不落入 InformationEntity/Event/PhysicalEntity 任一互斥顶层——不违反 AllDisjointClasses。"""
        anc = _ancestors(self.schema, SEMI.CompanyCode)
        self.assertIn(URIRef("urn:pxai:semi:Organization"), anc)
        self.assertEqual({PHYSICAL, EVENT, INFORMATION} & anc, set())

    def test_posting_actions_are_events(self) -> None:
        """过账/结转/发货/收入确认/清账等【动作】类上溯到 Event，绝不落进 InformationEntity。"""
        for name in ("PostingEvent", "CarryForwardEvent", "GoodsIssueEvent",
                     "RevenueRecognitionEvent", "ClearingEvent"):
            cls = SEMI[name]
            anc = _ancestors(self.schema, cls)
            self.assertIn(EVENT, anc, f"{name} 应上溯到 Event")
            self.assertNotIn(INFORMATION, anc, f"{name} 不应是 InformationEntity")

    def test_no_erp_class_double_placed_in_disjoint_tops(self) -> None:
        """红线守护：没有任何 ERP 类同时上溯到两个互斥顶层（否则 OWL 不一致、门禁物化会炸）。"""
        tops = {PHYSICAL, EVENT, INFORMATION}
        for cls in self.schema.subjects(RDF.type, OWL.Class):
            if not str(cls).startswith("urn:pxai:semi:"):
                continue
            hit = tops & _ancestors(self.schema, cls)
            self.assertLessEqual(len(hit), 1, f"{cls} 双挂互斥顶层 {hit}")


@unittest.skipIf(Graph is None, "semantic dependencies not installed")
class ErpInstanceProjectionTest(unittest.TestCase):
    """migrate 产物校验：ERP 实例经 YAML→current.ttl 通路落地。"""

    @classmethod
    def setUpClass(cls) -> None:
        if not CURRENT_TTL.is_file():
            raise unittest.SkipTest("current.ttl 未生成")
        cls.data = Graph()
        cls.data.parse(CURRENT_TTL, format="turtle")

    def test_erp_master_data_present(self) -> None:
        """财务主数据锚点（公司代码/科目表/GL 科目）与 O2C 单据（销售订单/发票）均已落图。"""
        for legacy_id in ("erp.company.cc1000", "erp.coa.ykint", "erp.gl.6001_revenue",
                          "erp.order.so2026_0001", "erp.billing.vf2026_0001", "erp.ar.ar2026_0001"):
            subject = URIRef(f"urn:pxai:semi:catalog:{legacy_id}")
            self.assertTrue(
                any(True for _ in self.data.triples((subject, RDF.type, None))),
                f"{legacy_id} 未落进 current.ttl",
            )

    def test_erp_typed_to_domain_classes(self) -> None:
        """ERP 实例被 typed 到其领域类（migrate TYPE_MAP 生效），而非仅裸 CatalogConcept。"""
        company = URIRef("urn:pxai:semi:catalog:erp.company.cc1000")
        types = set(self.data.objects(company, RDF.type))
        self.assertIn(SEMI.CompanyCode, types)
        revenue = URIRef("urn:pxai:semi:catalog:erp.gl.6001_revenue")
        self.assertIn(SEMI.GLAccount, set(self.data.objects(revenue, RDF.type)))


if __name__ == "__main__":
    unittest.main()
