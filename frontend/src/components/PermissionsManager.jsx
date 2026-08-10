import { useEffect, useState } from "react";
import { Shield, Loader2 } from "lucide-react";
import { api, formatApiErrorDetail } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger,
} from "@/components/ui/dialog";
import { toast } from "sonner";

const PERMISSION_LABELS = {
  can_chat: "Send chat & direct messages",
  can_upload_files: "Upload files",
  can_view_members_only: "View Members-Only channel",
  can_edit_calendar: "Create & edit calendar events",
  can_manage_todos: "Manage any task (not just their own)",
  can_delete_any_message: "Delete any chat message",
  can_delete_any_file: "Delete any shared file",
  can_manage_members: "Approve / add / remove members",
};

const PERMISSION_ORDER = [
  "can_chat",
  "can_upload_files",
  "can_view_members_only",
  "can_edit_calendar",
  "can_manage_todos",
  "can_delete_any_message",
  "can_delete_any_file",
  "can_manage_members",
];

export function PermissionsManager({ user, onSaved }) {
  const [open, setOpen] = useState(false);
  const [permissions, setPermissions] = useState({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open || !user?.id) return;
    setLoading(true);
    api
      .get(`/users/${user.id}/permissions`)
      .then(({ data }) => {
        setPermissions(data.permissions || {});
      })
      .catch((err) => {
        toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not load permissions");
      })
      .finally(() => setLoading(false));
  }, [open, user?.id]);

  const toggle = (key) => {
    setPermissions((p) => ({ ...p, [key]: !p[key] }));
  };

  const save = async () => {
    if (!user?.id) return;
    setSaving(true);
    try {
      await api.put(`/users/${user.id}/permissions`, { permissions });
      toast.success(`${user.name}'s permissions updated`);
      setOpen(false);
      onSaved?.();
    } catch (err) {
      toast.error(formatApiErrorDetail(err.response?.data?.detail) || "Could not save permissions");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" className="rounded-xl gap-1.5" data-testid={`permissions-button-${user.id}`}>
          <Shield className="h-4 w-4" /> Permissions
        </Button>
      </DialogTrigger>
      <DialogContent className="rounded-2xl max-w-lg">
        <DialogHeader>
          <DialogTitle className="font-heading">Edit Permissions for {user.name}</DialogTitle>
        </DialogHeader>
        {loading ? (
          <div className="flex items-center justify-center py-10">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <div className="space-y-4 py-2">
            {PERMISSION_ORDER.map((key) => (
              <Card key={key} className="p-3 rounded-xl flex items-center justify-between">
                <Label htmlFor={`perm-${user.id}-${key}`} className="text-sm font-medium cursor-pointer">
                  {PERMISSION_LABELS[key]}
                </Label>
                <Switch
                  id={`perm-${user.id}-${key}`}
                  checked={!!permissions[key]}
                  onCheckedChange={() => toggle(key)}
                  data-testid={`perm-${user.id}-${key}`}
                />
              </Card>
            ))}
            <Button onClick={save} disabled={saving} className="w-full rounded-xl">
              {saving ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : null}
              Save Permissions
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
