import { useState } from 'react';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuth } from '@/contexts/AuthContext';

interface Props {
  open: boolean;
  onOpenChange: (o: boolean) => void;
}

export function LoginDialog({ open, onOpenChange }: Props) {
  const { isLoggedIn, login, logout } = useAuth();
  const [user, setUser] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const onCancel = () => {
    setError(null);
    setUser('');
    setPassword('');
    onOpenChange(false);
  };

  const handleLogin = async () => {
    setError(null);
    if (!user || !password) {
      setError('User and Password are required');
      return;
    }
    setIsSubmitting(true);
    try {
      const ok = await login(user, password);
      if (!ok) {
        setError('Login failed');
        return;
      }
      // success: close dialog
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Login failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleLogout = async () => {
    setIsSubmitting(true);
    try {
      await logout();
      onOpenChange(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Logout failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>User Login</DialogTitle>
          <DialogDescription>
            Note: Valkey Navigator can be extended to use your preferred login and user management hooks. Implementation depends on your specific environment (e.g.: SSO, Cognito, other). Please extend the function ManageUserLogin and ManageUserLogout for your needs.
          </DialogDescription>
        </DialogHeader>

        {!isLoggedIn && (
          <div className="space-y-4">
            <div className="grid grid-cols-4 items-center gap-4">
              <Label htmlFor="user" className="text-right">User</Label>
              <Input id="user" className="col-span-3" value={user} onChange={(e) => setUser(e.target.value)} />
            </div>
            <div className="grid grid-cols-4 items-center gap-4">
              <Label htmlFor="password" className="text-right">Password</Label>
              <Input id="password" type="password" className="col-span-3" value={password} onChange={(e) => setPassword(e.target.value)} />
            </div>
            {error && (
              <div className="text-sm text-red-600">{error}</div>
            )}
          </div>
        )}

        <DialogFooter>
          {!isLoggedIn ? (
            <>
              <Button variant="default" onClick={handleLogin} disabled={isSubmitting}>Login</Button>
              <Button variant="secondary" onClick={onCancel} disabled={isSubmitting}>Cancel</Button>
            </>
          ) : (
            <>
              <Button variant="destructive" onClick={handleLogout} disabled={isSubmitting}>Logout</Button>
              <Button variant="secondary" onClick={onCancel} disabled={isSubmitting}>Cancel</Button>
            </>
          )}
        </DialogFooter>
            Note: This is the basic login dialog not yet extended. ANY user or password will work and are not stored or checked. Please extend as needed.
      </DialogContent>
    </Dialog>
  );
}
