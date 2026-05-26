import AdminDashboard from "@/components/admin/AdminDashboard";

export const metadata = {
  title: "Admin Configuration - Social Sentiment",
};

export default function AdminPage() {
  return (
    <main className="min-h-screen bg-[#050810] text-slate-300 p-8 selection:bg-indigo-500/30">
      <AdminDashboard />
    </main>
  );
}
