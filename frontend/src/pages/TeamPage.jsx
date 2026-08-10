import { useEffect, useState } from "react";
import { Users, Crown, Loader2, UserPlus, Trash2, UserCheck, Check, X } from "lucide-react";
import { api, formatApiErrorDetail } from "@/lib/api";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { useAuth } from "@/context/AuthContext";
import { PermissionsManager } from "@/components/PermissionsManager";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogTrigger,
} from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { toast } from "sonner";

const isElectron = typeof window !== "undefined" && window.electronAPI?.isElectron;

const emptyForm = { name: "", email: "", password: "", role: "member" };

export default function TeamPage() {
  const { user } = useAuth();
  const [users, setUsers] = useState(null);
  const [pending, setPending] = useState([]);
  const [pendingRoles, setPendingRoles] = useState({});
  const [busyId, setBusyId] = useState(null);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState(emptyForm);
  const [saving, setSaving] = useState(false);
  const [toDelete, setToDelete] = useState(null);

  const load = () => api.get("/users").then(({ data }) => setUsers(data)).catch(() => setUsers([]));
  const loadPending = () => api.get("/users/pending").then(({ data }) => setPending(data)).catch(() => setPending([]));
  useEffect(() => { load(); loadPending(); }, []);

  if (user?.role !== "owner") {
    return (
      <div className="max-w-3xl mx-auto p-6 lg:p-8">
        <Card className="p-8 rounded-2xl">
          <h1 className="font-heading text-2xl font-bold mb-2">Team Members</h1>
          <p className="text-muted-foreground">
            Only the owner can manage team permissions.
          </p>
        </Card>
      </div>
    );
  }

  const approve = async (u) => {
    setBusyId(u.id);
    try {
      const role = pendingRoles[u.id] || "member";
      const { data } = await api.post(`/users/${u.id}/approve`, { role });
      toast.success(`${u.name} approved`);
      setPending((p) => p.filter((x) => x.id !== u.id));
      setUsers((cur) => [...(cur || []), data]);
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not approve member");
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (u) => {
    setBusyId(u.id);
    try {
      await api.post(`/users/${u.id}/reject`);
      toast.success(`${u.name}'s request declined`);
      setPending((p) => p.filter((x) => x.id !== u.id));
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not decline request");
    } finally {
      setBusyId(null);
    }
  };

  const changeRole = async (id, role) => {
    try {
      await api.put(`/users/${id}/role`, { role });
      toast.success("Role updated");
      load();
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not update role");
    }
  };

  const createUser = async (e) => {
    e.preventDefault();
    setSaving(true);
    try {
      const { data } = await api.post("/users", form);
      toast.success(`${data.name} added to the team`);
      setUsers((u) => [...(u || []), data]);
      setForm(emptyForm);
      setOpen(false);
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not add member");
    } finally {
      setSaving(false);
    }
  };

  const removeUser = async () => {
    if (!toDelete) return;
    try {
      await api.delete(`/users/${toDelete.id}`);
      toast.success(`${toDelete.name} removed`);
      setUsers((u) => u.filter((x) => x.id !== toDelete.id));
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not remove member");
    } finally {
      setToDelete(null);
    }
  };

  return (
    <div className="max-w-4xl mx-auto p-6 lg:p-8 space-y-8" data-testid="team-page">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="h-11 w-11 rounded-xl bg-primary/10 text-primary flex items-center justify-center">
            <Users className="h-6 w-6" />
          </div>
          <div>
            <h1 className="font-heading text-3xl font-bold">Team Members</h1>
            <p className="text-muted-foreground">Add, remove, and set roles. Only you hold the owner role.</p>
          </div>
        </div>

        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button className="rounded-xl h-11 gap-2" data-testid="add-member-button">
              <UserPlus className="h-4 w-4" /> Add Member
            </Button>
          </DialogTrigger>
          <DialogContent className="rounded-2xl">
            <DialogHeader>
              <DialogTitle className="font-heading">Add Team Member</DialogTitle>
            </DialogHeader>
            <form onSubmit={createUser} className="space-y-4">
              <div className="space-y-2">
                <Label>Full name</Label>
                <Input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="Jordan Lee" className="rounded-xl" data-testid="member-name-input" />
              </div>
              <div className="space-y-2">
                <Label>Email</Label>
                <Input required type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })}
                  placeholder="jordan@email.com" className="rounded-xl" data-testid="member-email-input" />
              </div>
              <div className="space-y-2">
                <Label>Temporary password</Label>
                <Input required value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })}
                  placeholder="At least 6 characters" className="rounded-xl" data-testid="member-password-input" />
              </div>
              <div className="space-y-2">
                <Label>Role</Label>
                <Select value={form.role} onValueChange={(v) => setForm({ ...form, role: v })}>
                  <SelectTrigger className="rounded-xl" data-testid="member-role-select"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="member">Member</SelectItem>
                    <SelectItem value="mentor">Mentor</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <DialogFooter>
                <Button type="submit" disabled={saving} className="rounded-xl" data-testid="member-save-button">
                  {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : "Add Member"}
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>
      </div>

      {pending.length > 0 && (
        <div className="space-y-3" data-testid="pending-approvals">
          <div className="flex items-center gap-2">
            <UserCheck className="h-5 w-5 text-secondary" />
            <h2 className="font-heading text-xl font-bold">Pending Approvals</h2>
            <Badge className="rounded-lg bg-secondary text-secondary-foreground">{pending.length}</Badge>
          </div>
          <p className="text-sm text-muted-foreground">New people who requested to join. Approve to grant access.</p>
          {pending.map((u) => (
            <Card key={u.id} className="p-4 rounded-2xl flex flex-col sm:flex-row sm:items-center gap-4 border-secondary/40" data-testid={`pending-user-${u.id}`}>
              <Avatar className="h-11 w-11">
                <AvatarFallback className="bg-secondary text-secondary-foreground font-semibold">
                  {(u.name || "U").slice(0, 2).toUpperCase()}
                </AvatarFallback>
              </Avatar>
              <div className="flex-1 min-w-0">
                <p className="font-medium truncate">{u.name}</p>
                <p className="text-sm text-muted-foreground truncate">{u.email}</p>
              </div>
              <Select value={pendingRoles[u.id] || "member"} onValueChange={(v) => setPendingRoles((r) => ({ ...r, [u.id]: v }))}>
                <SelectTrigger className="w-32 rounded-xl" data-testid={`pending-role-${u.id}`}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="member">Member</SelectItem>
                  <SelectItem value="mentor">Mentor</SelectItem>
                </SelectContent>
              </Select>
              <div className="flex items-center gap-2">
                <Button onClick={() => approve(u)} disabled={busyId === u.id} size="sm"
                  className="rounded-xl gap-1.5" data-testid={`approve-user-${u.id}`}>
                  {busyId === u.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />} Approve
                </Button>
                <Button onClick={() => reject(u)} disabled={busyId === u.id} size="sm" variant="outline"
                  className="rounded-xl gap-1.5 text-destructive hover:text-destructive" data-testid={`reject-user-${u.id}`}>
                  <X className="h-4 w-4" /> Decline
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {users === null ? (
        <div className="flex justify-center py-20"><Loader2 className="h-8 w-8 animate-spin text-muted-foreground" /></div>
      ) : (
        <div className="space-y-3">
          {users.map((u) => (
            <Card key={u.id} className="p-4 rounded-2xl flex items-center gap-4" data-testid={`team-user-${u.id}`}>
              <Avatar className="h-11 w-11">
                <AvatarFallback className="bg-primary text-primary-foreground font-semibold">
                  {(u.name || "U").slice(0, 2).toUpperCase()}
                </AvatarFallback>
              </Avatar>
              <div className="flex-1 min-w-0">
                <p className="font-medium truncate flex items-center gap-2">
                  {u.name}
                  {u.role === "owner" && <Crown className="h-4 w-4 text-secondary" />}
                </p>
                <p className="text-sm text-muted-foreground truncate">{u.email}</p>
              </div>
              {u.role === "owner" ? (
                <Badge className="bg-secondary text-secondary-foreground rounded-lg">Owner</Badge>
              ) : (
                <>
                  <Select value={u.role} onValueChange={(v) => changeRole(u.id, v)}>
                    <SelectTrigger className="w-32 rounded-xl" data-testid={`role-select-${u.id}`}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="member">Member</SelectItem>
                      <SelectItem value="mentor">Mentor</SelectItem>
                    </SelectContent>
                  </Select>
                  {isElectron && <PermissionsManager user={u} onSaved={load} />}
                  <button onClick={() => setToDelete(u)} className="text-muted-foreground hover:text-destructive shrink-0"
                    data-testid={`delete-user-${u.id}`} aria-label="Remove member">
                    <Trash2 className="h-5 w-5" />
                  </button>
                </>
              )}
            </Card>
          ))}
        </div>
      )}

      <AlertDialog open={!!toDelete} onOpenChange={(o) => !o && setToDelete(null)}>
        <AlertDialogContent className="rounded-2xl">
          <AlertDialogHeader>
            <AlertDialogTitle>Remove {toDelete?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes {toDelete?.email} from the team. They will lose access immediately.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-xl">Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={removeUser} className="rounded-xl bg-destructive text-destructive-foreground hover:bg-destructive/90"
              data-testid="confirm-delete-user">
              Remove
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
